"""The actual cron script's systemctl child receives the owning user's bus env."""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

import pytest

from gateway.run import _profile_runtime_scope
from cron.scheduler_script import _run_job_script
from tools import process_registry as pr


def _probe(home, control):
    scripts = home / 'scripts'
    scripts.mkdir(parents=True)
    (home / 'config.yaml').write_text('terminal:\n  env: local\n', encoding='utf-8')
    script = scripts / 'probe.py'
    script.write_text('import subprocess, sys\n'
                      f'child = subprocess.run([{str(control)!r}, "--user", "show-environment"], '
                      'capture_output=True, text=True, timeout=5)\n'
                      'print(child.stdout)\nsys.exit(child.returncode)\n', encoding='utf-8')
    return script


def _control(path):
    path.write_text('#!' + sys.executable + '\nimport json, os, sys\n'
                    'print(json.dumps({"argv": sys.argv[1:], "home": os.getenv("HERMES_HOME"), '
                    '"runtime": os.getenv("XDG_RUNTIME_DIR"), "bus": os.getenv("DBUS_SESSION_BUS_ADDRESS")}))\n',
                    encoding='utf-8')
    path.chmod(0o755)
    return path


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('configured', [False, True])
def test_cron_script_systemctl_child_uses_owned_bus_and_profile(tmp_path, monkeypatch, configured):
    launch = tmp_path / 'launch'
    launch.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(launch))
    control = _control(tmp_path / 'fixture-systemctl')
    homes = {label: tmp_path / label for label in ('a', 'b')}
    scripts = {label: _probe(home, control) for label, home in homes.items()}
    # AF_UNIX has a short address limit; pytest's full tmp_path may exceed it.
    with tempfile.TemporaryDirectory(prefix='hbus-cron-', dir='/tmp') as directory:
        runtime = Path(directory)
        with socket.socket(socket.AF_UNIX) as bus:
            bus.bind(str(runtime / 'bus'))
            monkeypatch.setattr(pr, '_default_user_runtime_dir', lambda: runtime)
            if configured:
                monkeypatch.setenv('XDG_RUNTIME_DIR', str(runtime))
                monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/ignored-ambient-bus')
            else:
                monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
                monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS', raising=False)
            ambient = {key: os.getenv(key) for key in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS')}
            for label in ('a', 'b', 'a'):
                with _profile_runtime_scope(homes[label]):
                    ok, output = _run_job_script(str(scripts[label]), interpreter=sys.executable)
                    assert ok, output
                    receipt = json.loads(output)
                    assert receipt['argv'] == ['--user', 'show-environment']
                    assert receipt['home'] == str(homes[label])
                    assert receipt['runtime'] == str(runtime)
                    assert receipt['bus'] == f'unix:path={runtime / "bus"}'
                    assert os.environ['HERMES_HOME'] == str(launch)
            assert {key: os.getenv(key) for key in ambient} == ambient


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('invalid', ['missing-bus', 'regular-file', 'writable-directory'])
def test_cron_script_does_not_invent_bus_env_for_invalid_runtime(tmp_path, monkeypatch, invalid):
    home = tmp_path / 'profile'
    control = _control(tmp_path / 'fixture-systemctl')
    script = _probe(home, control)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    monkeypatch.delenv('DBUS_SESSION_BUS_ADDRESS', raising=False)
    with tempfile.TemporaryDirectory(prefix='hbus-cron-', dir='/tmp') as directory:
        runtime = Path(directory)
        with socket.socket(socket.AF_UNIX) as bus:
            if invalid == 'regular-file':
                (runtime / 'bus').write_text('not a socket', encoding='utf-8')
            elif invalid == 'writable-directory':
                bus.bind(str(runtime / 'bus'))
                runtime.chmod(0o777)
            monkeypatch.setattr(pr, '_default_user_runtime_dir', lambda: runtime)
            ok, output = _run_job_script(str(script), interpreter=sys.executable)
            assert ok, output
            receipt = json.loads(output)
            assert receipt['runtime'] is None
            assert receipt['bus'] is None
