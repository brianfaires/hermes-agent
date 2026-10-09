"""Real user-systemd qualification of the source guard; disposable units only."""
import argparse
import tempfile
import re
import hashlib

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import pytest

# Pytest never opts into live execution, even when this file is selected directly.
pytestmark = pytest.mark.skip(reason="standalone CLI opt-in required; no manager probes")
FAULTS = ('unguarded_turn_cap', 'before_arm', 'killed_after_switch',
          'target_import_error', 'missing_report', 'surviving_parent',
          'command_timeout_parent', 'model_failure_after_stop')
GUARD = Path(__file__).resolve().parents[2] / 'scripts/claude_release_switch/guarded_switch.py'


def run(argv, *, check=True, cwd=None, timeout=60):
    environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                   'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}',
                   'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_CONFIG_GLOBAL': '/dev/null'}
    return subprocess.run(argv, env=environment, cwd=cwd, text=True,
                          capture_output=True, timeout=timeout, check=check)


class OwnedManager:
    def __init__(self, prefix, runner=run):
        if not re.fullmatch(r'guarded-switch-test-[0-9a-f]{32}', prefix):
            raise ValueError('invalid disposable owner')
        self.units = {prefix + '-app.service', prefix + '-executor.service'}
        self.runner = runner

    def unit(self, unit):
        if unit not in self.units:
            raise ValueError('not an exact owned unit')

    def ctl(self, verb, unit, **kwargs):
        self.unit(unit)
        if verb not in ('show', 'start', 'stop', 'reset-failed'):
            raise ValueError('unsupported unit action')
        return self.runner(['/usr/bin/systemctl', '--user', verb, unit], **kwargs)

    def show(self, unit):
        return dict(line.split('=', 1) for line in self.ctl('show', unit).stdout.splitlines()
                    if '=' in line)

    def start(self, unit, properties, command):
        self.unit(unit)
        return self.runner(['/usr/bin/systemd-run', '--user', '--unit=' + unit,
                            *['--property=' + p for p in properties], *command])


def wait_for(fn, timeout=40):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = fn()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError('bounded disposable-service condition not reached')


@pytest.mark.parametrize('fault', FAULTS)
def test_real_systemd_guarded_switch(tmp_path, fault):
    pytest.skip('standalone CLI opt-in required; no manager probes')


def qualify(tmp_path, fault):
    # No production name or path can enter any mutating service command.
    prefix = 'guarded-switch-test-' + uuid.uuid4().hex
    gateway = prefix + '-app.service'
    executor = prefix + '-executor.service'
    manager = OwnedManager(prefix)
    show = manager.show
    root = tmp_path / 'fixture'
    root.mkdir(mode=0o700)
    repo = root / 'repo'
    repo.mkdir()
    private = root / 'private'
    private.mkdir(mode=0o700)
    state = private / 'state'
    state.mkdir(mode=0o700)
    (state / 'lock').touch(mode=0o600)
    guard = private / 'guarded_switch.py'
    shutil.copyfile(GUARD, guard)
    guard.chmod(0o600)
    python = str(Path(sys.executable).resolve())

    def git(*args):
        return run(['/usr/bin/git', '-C', str(repo), *args]).stdout.strip()

    def source(branch):
        return {'branch': branch, 'sha': git('rev-parse', branch),
                'tree': git('rev-parse', branch + '^{tree}')}

    git('init', '-b', 'good')
    git('config', 'user.name', 'Disposable guard fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (repo / 'app.py').write_text("VALUE = 'good'\n")
    git('add', 'app.py')
    git('commit', '-m', 'good')
    good = source('good')
    git('switch', '-c', 'target')
    (repo / 'app.py').write_text("VALUE = 'target'\n")
    git('commit', '-am', 'target')
    target = source('target')
    git('switch', 'good')
    original_refs = git('show-ref')
    smoke = private / 'smoke.py'
    smoke.write_text(
        'import json, os, pathlib, signal, sys, time\n'
        'repo = pathlib.Path(sys.argv[1])\n'
        'sys.path.insert(0, str(repo))\nimport app\n'
        f'fault = {fault!r}\n'
        f'state = pathlib.Path({str(state / "transaction.json")!r})\n'
        'phase = json.loads(state.read_text())["phase"] if state.exists() else "preflight"\n'
        'if app.VALUE == "target" and phase == "stopped":\n'
        '    if fault in ("killed_after_switch", "surviving_parent"): os.kill(os.getppid(), signal.SIGKILL)\n'
        '    if fault == "command_timeout_parent": time.sleep(120)\n'
        '    if fault == "model_failure_after_stop": os.kill(os.getppid(), signal.SIGKILL)\n'
        '    if fault == "target_import_error": raise RuntimeError("fixture import failure")\n')
    smoke.chmod(0o600)
    # Toy service records what it actually loaded; never imports Hermes.
    server = private / 'serve.py'
    health = private / 'health.json'
    server.write_text(
        'import json, os, pathlib, signal, sys\n'
        f'sys.path.insert(0, {str(repo)!r})\nimport app\n'
        f'pathlib.Path({str(health)!r}).write_text(json.dumps({{"pid":os.getpid(),"value":app.VALUE}}))\n'
        'while True: signal.pause()\n')
    server.chmod(0o600)
    manifest = private / 'manifest.json'
    try:
        # The executor Wants dependency pins this transient definition while stopped.
        # RemainAfterExit also retains it if the toy process exits normally.
        manager.start(gateway, ['Type=exec', 'Restart=no', 'RemainAfterExit=yes',
                                'KillMode=control-group', 'SendSIGKILL=yes',
                                'TimeoutStopSec=5', 'RuntimeMaxSec=600', 'UMask=0077'],
                      [python, '-I', '-S', '-B', str(server)])
        baseline = show(gateway)
        wait_for(lambda: health.exists() and json.loads(health.read_text())['value'] == 'good')
        if fault == 'unguarded_turn_cap':
            # Negative control: the former model-only topology strands the toy
            # app after a successful stop and a terminal model-like exit.
            driver = private / 'unguarded_exit.py'
            driver.write_text('import subprocess, sys, time, pathlib\n'
                              f'subprocess.run(["/usr/bin/systemctl", "--user", "stop", {gateway!r}], check=True)\n'
                              'sys.exit(33)\n')
            driver.chmod(0o600)
            manager.start(executor, ['Type=exec', 'Restart=no', 'RuntimeMaxSec=90',
                                     'TimeoutStopSec=5', 'KillMode=control-group', 'Wants=' + gateway],
                          [python, '-I', '-S', '-B', str(driver)])
            final = wait_for(lambda: show(executor).get('SubState') == 'failed' and show(executor))
            assert final['ExecMainStatus'] == '33'
            assert show(gateway)['ActiveState'] == 'inactive'
            assert not (state / 'transaction.json').exists()
            print(json.dumps({'fault': fault, 'negative_control': 'stopped_and_not_restored',
                              'executor': executor, 'invocation': final['InvocationID']}))
            return
        m = {'repo': str(repo), 'target': target, 'known_good': good,
             'service': gateway, 'baseline_invocation': baseline['InvocationID'],
             'executor': executor, 'state_dir': str(state), 'interpreter': python,
             'smoke_argv': [python, '-I', '-S', '-B', str(smoke), str(repo)],
             'smoke_sha256': hashlib.sha256(smoke.read_bytes()).hexdigest()}
        manifest.write_text(json.dumps(m))
        manifest.chmod(0o600)
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        hook = f'{python} -I -S -B {guard} recover {manifest} --sha256 {digest}'
        command = ['/bin/false'] if fault == 'before_arm' else [
            python, '-I', '-S', '-B', str(guard), 'run', str(manifest), '--sha256', digest]
        if fault in ('missing_report', 'surviving_parent', 'command_timeout_parent', 'model_failure_after_stop'):
            driver = private / 'model_exit.py'
            driver.write_text('import subprocess, sys, time, pathlib\n'
                              f'rc = subprocess.run({command!r}).returncode\n'
                              f'pathlib.Path({str(private / "child-returned")!r}).write_text(str(rc))\n'
                              + ('time.sleep(180)\n' if fault in ('surviving_parent', 'command_timeout_parent')
                                 else 'sys.exit(33 if rc == 0 else rc)\n'))
            driver.chmod(0o600)
            command = [python, '-I', '-S', '-B', str(driver)]
        manager.start(executor, ['Type=exec', 'ExitType=main', 'Restart=no',
                                 'RemainAfterExit=no', 'RuntimeMaxSec=90', 'Wants=' + gateway,
                                 'RuntimeRandomizedExtraSec=0', 'KillMode=control-group',
                                 'SendSIGKILL=yes', 'FinalKillSignal=SIGKILL',
                                 'TimeoutStopFailureMode=terminate', 'TimeoutStopSec=300',
                                 'UMask=0077', 'ExecStopPost=' + hook], command)
        state_file = state / 'transaction.json'
        if fault in ('surviving_parent', 'command_timeout_parent'):
            returned = private / 'child-returned'
            wait_for(returned.exists, timeout=80)
            assert int(returned.read_text()) != 0
            assert int(show(executor)['MainPID']) > 0  # independent model survives
            assert show(gateway)['MainPID'] == '0'
            if fault == 'command_timeout_parent':
                assert json.loads(state_file.read_text())['phase'] == 'awaiting_fence'
            # No manual stop: only the finite executor runtime can fence parent.
        wait_for(lambda: show(executor).get('ActiveState') in ('inactive', 'failed'), timeout=420)
        observed = show(gateway)
        assert observed['ActiveState'] == 'active'
        expected = 'target' if fault == 'missing_report' else 'good'
        loaded = wait_for(lambda: health.exists() and
                         json.loads(health.read_text()).get('pid') == int(observed['MainPID']) and
                         json.loads(health.read_text()))
        assert loaded['value'] == expected
        assert git('branch', '--show-current') == ('target' if expected == 'target' else 'good')
        assert git('show-ref') == original_refs
        assert not (private / 'RESULT.md').exists()
        if fault == 'before_arm':
            assert not state_file.exists()
            assert observed['InvocationID'] == baseline['InvocationID']
            transaction = {'phase': 'not_armed'}
        else:
            transaction = json.loads(state_file.read_text())
            assert transaction['phase'] == 'complete', transaction
            assert transaction['outcome'] == ('switched' if expected == 'target' else 'recovered')
            assert observed['InvocationID'] == transaction['gateway_invocation']
        print(json.dumps({'fault': fault, 'gateway': gateway, 'executor': executor,
                          'guard_sha256': hashlib.sha256(guard.read_bytes()).hexdigest(),
                          'gateway_invocation': observed['InvocationID'],
                          'transaction': transaction, 'actual_loaded': loaded}))
    finally:
        # Bounded exact-unit cleanup; no manager files or manager-wide actions.
        for unit in (executor, gateway):
            manager.ctl('stop', unit, check=False, timeout=660)
            manager.ctl('reset-failed', unit, check=False)


def main(argv=None, qualify_case=qualify):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-disposable-user-systemd', action='store_true')
    parser.add_argument('--case', choices=FAULTS, required=True)
    args = parser.parse_args(argv)
    if not args.allow_disposable_user_systemd:
        parser.error('explicit --allow-disposable-user-systemd required before any probe')
    with tempfile.TemporaryDirectory(prefix='guarded-systemd-') as directory:
        qualify_case(Path(directory), args.case)


if __name__ == '__main__':
    main()
