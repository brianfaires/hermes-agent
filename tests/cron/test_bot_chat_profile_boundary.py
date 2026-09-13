"""Capture the real Bot Chat spawn and run only CLI bootstrap in its child env."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from agent import secret_scope as ss
from cron import scheduler
from gateway.session_context import _VAR_MAP
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


@pytest.mark.parametrize('source,destination,multiplex', [
    ('alpha', '', True), ('default', '', True), ('alpha', 'beta', True),
    ('alpha', 'Default', True), ('alpha', '', False), ('default', '', False),
])
@pytest.mark.parametrize('home_mode', ['real', 'profile'])
def test_child_bootstrap_owns_source_or_explicit_target(tmp_path, monkeypatch, source, destination, multiplex, home_mode):
    root = tmp_path / 'custom deployment'
    homes = {'default': root, **{n: root / 'profiles' / n for n in ('alpha', 'beta')}}
    for name, h in homes.items():
        (h / 'home').mkdir(parents=True)
        (h / '.env').write_text(f'OPENAI_API_KEY=fake-{name}\n')
    (root / 'active_profile').write_text('beta')
    if not multiplex:
        # A standalone service can provide its only credential via process env.
        (homes[source] / '.env').unlink()
    target = (destination or source).lower()
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_REAL_HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    launch = root if multiplex else homes[source]
    monkeypatch.setenv('HERMES_HOME', str(launch))
    monkeypatch.setenv('TERMINAL_HOME_MODE', home_mode)
    monkeypatch.setenv('OPENAI_API_KEY', 'fake-process-only')
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'fake-sender-token')
    monkeypatch.setenv('UNSEEN_SENDER_SECRET', 'fake-sender-only')
    monkeypatch.setenv('HERMES_PROFILE', 'launch')
    monkeypatch.setenv('HERMES_PROFILE_NAME', 'launch')
    monkeypatch.setenv('HERMES_SESSION_CHAT_ID', 'sender-chat')
    monkeypatch.setattr(ss, '_MULTIPLEX_ACTIVE', multiplex)
    monkeypatch.setattr(scheduler.shutil, 'which', lambda _: None)
    before = dict(os.environ)
    captured = {}
    real_run = subprocess.run
    repo = str(Path(__file__).resolve().parents[2])
    def capture(argv, **kw):
        captured.update(argv=argv, env=kw['env'])
        # Only import the real CLI entry module; never call main()/chat.
        guard = textwrap.dedent(f'''
            import os, sys
            sys.dont_write_bytecode = True
            scratch = {str(tmp_path)!r}
            repo = {repo!r}
            def inside(path, root):
                return path == root or path.startswith(root + os.sep)
            def guard(event, args):
                if event in ('socket.connect', 'socket.bind', 'socket.getaddrinfo',
                             'socket.sendto', 'subprocess.Popen', 'os.system',
                             'os.exec', 'os.posix_spawn', 'os.kill', 'os.killpg'):
                    raise PermissionError('fixture child: external activity denied')
                if event in ('os.remove', 'os.mkdir', 'os.rmdir', 'os.chmod',
                             'os.rename', 'os.link', 'os.symlink'):
                    paths = args[:2] if event in ('os.rename', 'os.link', 'os.symlink') else args[:1]
                    for value in paths:
                        if not inside(os.path.realpath(os.fsdecode(value)), scratch):
                            raise PermissionError('fixture child: non-fixture mutation denied')
                if event == 'open' and isinstance(args[0], (str, bytes)):
                    path = os.path.realpath(os.fsdecode(args[0]))
                    if inside(path, scratch):
                        return
                    flags = args[2]
                    writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
                    if writing or os.path.basename(path) in ('.env', '.op.env', 'auth.json', 'credentials.json'):
                        raise PermissionError('fixture child: credential/write access denied')
                    if not any(inside(path, root) for root in (repo, sys.prefix, sys.base_prefix, '/usr', '/lib', '/lib64', '/etc/ssl')):
                        raise PermissionError('fixture child: non-fixture read denied')
            sys.addaudithook(guard)
        ''')
        code = guard + (
            'import sys,os,json; '
            f'sys.path.insert(0,{repo!r}); '
            f'sys.argv={["hermes", *argv[3:]]!r}; '
            'import hermes_cli.main; '
            'print(json.dumps({k:os.environ.get(k) for k in '
            '["HERMES_HOME","HOME","OPENAI_API_KEY"]}))'
        )
        result = real_run([sys.executable, '-c', code], env=kw['env'], capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        captured['bootstrap'] = json.loads(result.stdout.splitlines()[-1])
        return subprocess.CompletedProcess(argv, 0, '', '')
    monkeypatch.setattr(scheduler.subprocess, 'run', capture)
    ht = set_hermes_home_override(str(homes[source])) if multiplex else None
    st = ss.set_secret_scope(ss.build_profile_secret_scope(homes[source]))
    try:
        assert scheduler._deliver_to_bot_chat({'id': 'fixture', 'name': 'Fixture'}, 'output', destination) is None
    finally:
        ss.reset_secret_scope(st)
        if ht is not None:
            reset_hermes_home_override(ht)
    env = captured['env']; argv = captured['argv']
    assert argv[argv.index('-p') + 1] == target
    assert env['HERMES_HOME'] == str(homes[target])
    assert env['HOME'] == str(homes[target] / 'home' if home_mode == 'profile' else tmp_path)
    assert all(k not in env for k in _VAR_MAP)
    assert 'HERMES_PROFILE' not in env and 'HERMES_PROFILE_NAME' not in env
    assert 'TELEGRAM_BOT_TOKEN' not in env
    if multiplex or target != source:
        assert env['OPENAI_API_KEY'] == f'fake-{target}'
        assert 'UNSEEN_SENDER_SECRET' not in env
    else:
        assert env['OPENAI_API_KEY'] == 'fake-process-only'
    assert captured['bootstrap']['HERMES_HOME'] == str(homes[target])
    assert captured['bootstrap']['OPENAI_API_KEY'] == (
        f'fake-{target}' if multiplex else 'fake-process-only'
    )
    assert dict(os.environ) == before


@pytest.mark.parametrize('destination', ['missing', 'deleted', '../malformed'])
def test_missing_target_fails_before_child(tmp_path, monkeypatch, destination):
    if destination == 'deleted':
        from hermes_constants import mark_named_profile_deleted
        home = tmp_path / 'profiles' / destination
        home.mkdir(parents=True)
        mark_named_profile_deleted(home)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(scheduler.subprocess, 'run', lambda *a, **kw: pytest.fail('must not spawn'))
    error = scheduler._deliver_to_bot_chat({'id': 'fixture'}, 'output', destination)
    assert error and 'bot-chat delivery failed' in error
