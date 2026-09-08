#!/usr/bin/env python3
"""Disposable Git + user-systemd application, never a real gateway adapter.

The inline windows-footgun suppressions in this Linux/systemd fixture mark
deliberate POSIX UID, session, signal, and user-service behavior.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from controller import (BASE_ENV, GIT, PYTHON, RECEIPTS, Run, command_plan,
                        artifact_uid, digest, durable, encoded, git, loads, private, proc, raw,
                        require, revision, show, system_env)


PREFIX = 't-acd2041b-'


def new(root, operation='stage', model='fake', fault_point='', fault_kind=''):
    os.umask(0o077)
    root = Path(root).absolute()
    require(root.name.startswith(PREFIX) and not root.exists(), 'new task-named sandbox required')
    root.mkdir(mode=0o700)
    for name in ('runtime', 'run', 'repo', 'remote.git', 'git-home'):
        (root / name).mkdir(mode=0o700)
    durable(root / 'sandbox.json', {'task': 't_acd2041b', 'disposable': True}, exclusive=True)
    for name in ('controller.py', 'fixture.py', 'health.py'):
        dest = root / 'runtime' / name
        dest.write_bytes(Path(__file__).with_name(name).read_bytes())
        dest.chmod(0o600)
    (root / 'runtime/runbook.md').write_bytes(Path(__file__).with_name('README.md').read_bytes())
    (root / 'runtime/runbook.md').chmod(0o600)
    (root / 'sole-writer.lock').touch(mode=0o600)
    lock_st = (root / 'sole-writer.lock').stat()
    repo = str(root / 'repo')
    remote = str(root / 'remote.git')
    git(repo, 'init', '-b', 'main')
    git(remote, 'init', '--bare', '-b', 'main')
    git(repo, 'config', 'user.name', 'Disposable test')
    git(repo, 'config', 'user.email', 'sandbox@example.invalid')
    (Path(repo) / 'app.txt').write_text('known-good\n', encoding='utf-8')
    git(repo, 'add', 'app.txt')
    git(repo, 'commit', '-m', 'known-good fixture')
    old = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'branch', 'rollback')
    git(repo, 'switch', '-c', 'staging')
    (Path(repo) / 'app.txt').write_text('candidate\n', encoding='utf-8')
    git(repo, 'commit', '-am', 'candidate fixture')
    candidate = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'push', remote, 'main', 'staging')
    if operation == 'stage':
        git(repo, 'switch', 'main')
    current_branch = 'main' if operation == 'stage' else 'staging'
    current_sha = old if operation == 'stage' else candidate
    launcher = str(root / 'runtime' / 'fixture.py')
    unit = root.name + '-gateway.service'
    start_service(root)
    now = int(time.time())
    st = Path(repo).stat()
    packet = {
        'version': 2, 'command_timeout': 8, 'installation': str(root / 'runtime'), 'lock': str(root / 'sole-writer.lock'),
        'profile': 'simulated-application', 'repo': repo, 'remote': remote,
        'git_route': {'home': str(root / 'git-home'), 'helper': '', 'ssh': ''},
        'git_config': git(repo, 'config', '--local', '--list').splitlines(),
        'extra_refs': [], 'untracked': [], 'checks': {},
        'drain_path': str(root / 'run/drain-proof.json'), 'health_path': str(root / 'run/runtime-health.json'),
        'repo_identity': {'device': st.st_dev, 'inode': st.st_ino}, 'operation': operation,
        'lock_identity': {'device': lock_st.st_dev, 'inode': lock_st.st_ino},
        'order': 'SIMULATED disposable sandbox: ' + operation + '; verification owner and sole writer: fixture creator. One batch only; rollback source only, no persistent-state restore.',
        'runbook': {'path': str(root / 'runtime/runbook.md'), 'sha256': digest((root / 'runtime/runbook.md').read_bytes())},
        'current': revision(repo, current_sha, current_branch),
        'candidate': revision(repo, candidate, 'staging'),
        'rollback': revision(repo, old, 'rollback'),
        'local': {'main': old, 'staging': candidate, 'rollback': old},
        'authoritative': {'main': old, 'staging': candidate},
        'unit': unit, 'unit_definition_sha256': digest(raw(['/usr/bin/systemctl', '--user', 'cat', unit], '/', env=system_env()).encode()), 'controller_unit': root.name + '-controller.service',
        'launcher': launcher, 'launcher_sha256': digest(Path(launcher).read_bytes()),
        'baseline': proc(int(show(unit)['MainPID'])),
        'window': {'start': now - 10, 'abort': now + 1200, 'expiry': now + 1570,
                   'recovery_deadline': now + 1630, 'reserve': 120},
        'receipts': {}, 'commands': [], 'recovery': [],
        'model': {'kind': model, 'name': 'claude-sonnet-5', 'timeout': 60 if model == 'claude' else 1},
        'artifacts': [{'path': str(root / 'runtime' / name),
                       'sha256': digest((root / 'runtime' / name).read_bytes())}
                      for name in ('controller.py', 'fixture.py', 'health.py', 'runbook.md')],
    }
    for executable in (GIT, PYTHON, '/usr/bin/systemctl', '/usr/bin/systemd-run', show(unit)['FragmentPath']):
        packet['artifacts'].append({'path': executable, 'sha256': digest(Path(executable).read_bytes())})
    for artifact in packet['artifacts']:
        st = Path(artifact['path']).stat()
        artifact.update(uid=artifact_uid(artifact['path']), mode=st.st_mode & 0o777)
    facts = {
        'backup': {'coverage': 'no persistent fixture data', 'restore_verified': 'simulated restore'},
        'compatibility': {k: 'simulated compatible' for k in ('candidate', 'rollback', 'dependencies', 'persistent_state')},
        'drain': {'mechanism': 'fixture has no jobs', 'all_source_consumers': 'fixture service only'},
        'sole_writer': {k: 'fixture creator external hold' for k in ('owner', 'hold', 'writers', 'recovery_handoff')},
        'git_credentials': {'route': digest(encoded(packet['git_route'])), 'actor': 'fixture creator', 'permission_verified': 'owned bare remote'},
        'authority': {k: 'SIMULATED fixture authority' for k in ('approver', 'provenance', 'batch', 'recovery', 'contact')},
        'readiness': {k: 'SIMULATED no inference' for k in ('probe_path', 'probe_sha256', 'auth_method', 'model', 'cli_sha256')},
        'ci': {'exact_sha': candidate, 'result': 'simulated pass'},
        'review': {'exact_sha': candidate, 'verdict': 'simulated pass'},
    }
    for kind in RECEIPTS:
        path = root / 'run' / (kind + '.json')
        durable(path, {'order': packet['order'], 'kind': kind, 'candidate': candidate, 'rollback': old,
                       'repo': repo, 'unit': unit, 'profile': packet['profile'],
                       'expires': packet['window']['recovery_deadline'], 'approved': True, 'facts': facts[kind],
                       'evidence': 'SIMULATED disposable sandbox: no real data, application or CI approval.'})
        packet['receipts'][kind] = {'path': str(path), 'sha256': digest(path.read_bytes())}
    for name in ('drain', 'offline', 'smoke', 'recover-offline', 'recover-smoke'):
        packet['checks'][name] = {'id': name, 'argv': [PYTHON, launcher, str(root), name],
                                  'cwd': str(root / 'runtime'), 'env': BASE_ENV.copy(), 'timeout': 8}
    durable(root / 'fault.json', {'point': fault_point, 'kind': fault_kind})
    packet['commands'], packet['recovery'] = command_plan(packet)
    durable(root / 'run' / 'packet.json', packet, exclusive=True)
    authority = digest((root / 'run' / 'packet.json').read_bytes())
    durable(root / 'run' / 'digest.json', {'sha256': authority}, exclusive=True)
    return packet, authority


def start_service(root):
    unit = root.name + '-gateway.service'
    unit_path = Path('/run/user') / str(os.getuid()) / 'systemd/user' / unit  # windows-footgun: ok
    if not unit_path.exists():
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text('[Unit]\nDescription=Disposable release controller test\n[Service]\n'
                             'Type=exec\nRestart=no\nUMask=0077\nKillMode=control-group\nTimeoutStopSec=3\n'
                             'RuntimeMaxSec=1800\nStandardOutput=null\nStandardError=null\n'
                             'WorkingDirectory=' + str(root / 'runtime') + '\n'
                             'ExecStart=' + PYTHON + ' ' + str(root / 'runtime/fixture.py') + ' ' + str(root) + ' serve\n',
                             encoding='utf-8')
        unit_path.chmod(0o600)
        raw(['/usr/bin/systemctl', '--user', 'daemon-reload'], root, 10, system_env())
    raw(['/usr/bin/systemctl', '--user', 'start', unit], root, 5, system_env())
    wait_health(root)


def wait_health(root):
    unit = root.name + '-gateway.service'
    end = time.monotonic() + 5
    while time.monotonic() < end:
        if (root / 'health.json').exists():
            h = loads((root / 'health.json').read_bytes())
            if str(h['pid']) == show(unit)['MainPID']:
                return
        time.sleep(0.05)
    raise RuntimeError('fixture startup failed')


def validate_root(root):
    require(root.is_absolute() and root.resolve() == root and root.name.startswith(PREFIX), 'sandbox path')
    private(root, True)
    require(loads(private(root / 'sandbox.json').read_bytes()) == {'task': 't_acd2041b', 'disposable': True}, 'sandbox marker')
    require((root / 'repo').resolve() == root / 'repo', 'sandbox repo symlink')


def lifecycle(root, action):
    validate_root(root)
    unit = root.name + '-gateway.service'
    if action == 'stall':
        os.setsid()  # windows-footgun: ok
        time.sleep(120)
    elif action == 'serve':
        repo = str(root / 'repo')
        sha = git(repo, 'rev-parse', 'HEAD')
        if (root / 'fault.json').exists():
            fault = loads((root / 'fault.json').read_bytes())
            packet = loads((root / 'run/packet.json').read_bytes())
            require(not (fault['kind'] == 'startup' and sha != packet['rollback']['sha']), 'simulated startup failure')
        files = revision(repo, sha, '')['files']
        identity = proc(os.getpid())
        loaded = [{'module': 'fixture_app', 'path': 'app.txt', 'sha256': digest((root / 'repo/app.txt').read_bytes())}]
        durable(root / 'health.json', {'pid': os.getpid(), 'starttime': identity['starttime'],
                                     'sha': sha, 'source': repo, 'bytes': files, 'loaded': loaded, 'healthy': True,
                                     'executable_sha256': digest(Path('/proc/self/exe').read_bytes()),
                                     'platform': 'ok', 'scheduler': 'ok',
                                     'persistence': 'ok', 'sessions': 'ok'})
        # The file captures source loaded at process startup, not mutable HEAD read later.
        while True:
            signal.pause()
    elif action in ('drain', 'busy'):
        identity = proc(int(show(unit)['MainPID']))
        print(json.dumps({'active_jobs': 1 if action == 'busy' else 0, 'unit': unit, 'pid': identity['pid'],
                          'starttime': identity['starttime'], 'observed': int(time.time())}))
    elif action == 'stop':
        if show(unit)['ActiveState'] == 'inactive':
            return
        raw(['/usr/bin/systemctl', '--user', 'stop', unit], root, 5, system_env())
    elif action in ('start', 'recover-start'):
        p = loads((root / 'run' / 'packet.json').read_bytes())
        require(not (action == 'start' and loads((root / 'fault.json').read_bytes()) == {'point': 'start', 'kind': 'startup'}), 'simulated startup refusal')
        start_service(root)
    elif action in ('offline', 'recover-offline'):
        if action == 'offline':
            inject_fault(root)
        require((root / 'repo' / 'app.txt').read_text(encoding='utf-8') in ('candidate\n', 'known-good\n'), 'offline contract')
    elif action in ('smoke', 'recover-smoke'):
        p = loads((root / 'run' / 'packet.json').read_bytes())
        require(not (action == 'smoke' and loads((root / 'fault.json').read_bytes()) == {'point': 'smoke', 'kind': 'smoke'}), 'simulated smoke failure')
        wait_health(root)
        health = loads(private(root / 'health.json').read_bytes())
        require(health['healthy'] is True and show(unit)['MainPID'] == str(health['pid']), 'fixture smoke failure')
        from health import read_health
        durable(root / 'startup.json', {k: health[k] for k in ('pid', 'starttime', 'sha', 'source', 'bytes', 'loaded', 'executable_sha256')})
        durable(root / 'live.json', {**{k: health[k] for k in ('pid', 'starttime', 'platform', 'scheduler', 'persistence', 'sessions')},
                                    'served_profile_homes': {'default': str(root)}, 'observed': int(time.time())})
        print(json.dumps(read_health(root / 'startup.json', root / 'live.json', unit)))
    elif action == 'switch-timeout':
        p = loads((root / 'run' / 'packet.json').read_bytes())
        target = 'main' if p['operation'] == 'promote' else 'staging'
        git(str(root / 'repo'), 'switch', target)
        child = subprocess.Popen([PYTHON, '-c', 'import os,time; os.setsid(); time.sleep(120)'], env=BASE_ENV)  # windows-footgun: ok
        durable(root / 'run' / 'descendant.json', {'pid': child.pid})
        time.sleep(120)
    elif action == 'cleanup':
        # Unit names derive solely from a private task-named fixture; no arbitrary services.
        for suffix in ('controller', 'gateway'):
            name = root.name + '-' + suffix + '.service'
            if show(name)['ActiveState'] != 'inactive':
                raw(['/usr/bin/systemctl', '--user', 'stop', name], root, 130, system_env())
            subprocess.run(['/usr/bin/systemctl', '--user', 'reset-failed', name], env=system_env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        unit_path = Path('/run/user') / str(os.getuid()) / 'systemd/user' / unit  # windows-footgun: ok
        unit_path.unlink(missing_ok=True)
        raw(['/usr/bin/systemctl', '--user', 'daemon-reload'], root, 10, system_env())
    else:
        raise ValueError('unknown fixture action')


def inject_fault(root):
    fault = loads((root / 'fault.json').read_bytes())
    kind = fault['kind']
    if kind == 'supervisor-loss':
        os.kill(os.getppid(), signal.SIGKILL)  # windows-footgun: ok
        time.sleep(120)
    elif kind == 'model-loss':
        raise RuntimeError('simulated model/startup failure')
    elif kind in ('timeout', 'escape', 'expiry'):
        if kind == 'expiry':
            os.kill(os.getppid(), signal.SIGSTOP)
        child = subprocess.Popen([PYTHON, str(root / 'runtime/fixture.py'), str(root), 'stall'], env=BASE_ENV)
        durable(root / 'run/descendant.json', {'pid': child.pid})
        time.sleep(120)
    elif kind == 'byte-drift':
        (root / 'repo/app.txt').write_text('unapproved bytes\n', encoding='utf-8')
        raise RuntimeError('simulated byte drift')
    elif kind == 'ref-drift':
        git(str(root / 'repo'), 'branch', 'unapproved', 'rollback')
        raise RuntimeError('simulated ref drift')


def ssh_transport(root):
    import shlex
    validate_root(root)
    command = shlex.split(sys.argv[-1])
    require(len(command) == 2 and command[0] in ('git-upload-pack', 'git-receive-pack')
            and command[1] == str(root / 'remote.git'), 'fixture SSH command')
    durable(root / 'ssh-environment.json', {'home': os.environ.get('HOME'),
            'inherited_credentials': any(k.endswith(('_TOKEN', '_API_KEY')) for k in os.environ)})
    os.execve('/usr/bin/' + command[0], command, BASE_ENV)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('action', choices=('create', 'stall', 'busy', 'serve', 'drain', 'stop', 'start', 'offline', 'smoke', 'recover-start', 'recover-offline', 'recover-smoke', 'switch-timeout', 'cleanup'))
    parser.add_argument('--operation', choices=('stage', 'promote', 'rollback'), default='stage')
    parser.add_argument('--model', choices=('fake',), default='fake')
    parser.add_argument('--fault-point', default='')
    parser.add_argument('--fault-kind', default='')
    args = parser.parse_args()
    if args.action == 'create':
        p, authority = new(args.root, args.operation, args.model, args.fault_point, args.fault_kind)
        print(json.dumps({'packet': str(args.root.absolute() / 'run' / 'packet.json'), 'authority': authority,
                          'controller': str(args.root.absolute() / 'runtime' / 'controller.py')}))
    else:
        lifecycle(args.root.absolute(), args.action)


if __name__ == '__main__':
    main()
