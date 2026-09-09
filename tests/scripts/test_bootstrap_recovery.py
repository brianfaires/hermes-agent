"""Disposable same-service bootstrap recovery qualification.

The tests use transient task-named user-systemd units and private files only.
They do not write persistent unit definitions, call daemon-reload, or touch live
Hermes source/configuration.
"""
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts' / 'claude_release_switch'
sys.path.insert(0, str(SCRIPTS))
import bootstrap_recovery as b
import bootstrap_install_packet_template as template
import bootstrap_runtime_bindings as bindings
import controller as c
sys.path.remove(str(SCRIPTS))

PREFIX = 't-e8912230-'


def wait_for(check, timeout=35):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError('bounded observable condition not reached')


@pytest.fixture
def bootstrap_sandbox(tmp_path):
    try:
        c.raw(['/usr/bin/systemctl', '--user', 'show', '--property=Version'], '/', env=c.system_env())
    except Exception:
        pytest.skip('real user-systemd unavailable; bootstrap topology not qualified')
    roots = []

    def create(fault=''):
        root = tmp_path / (PREFIX + uuid.uuid4().hex[:12])
        roots.append(root)
        return new_bootstrap_root(root, fault)

    yield create

    for root in roots:
        for suffix in ('controller', 'gateway'):
            unit = root.name + '-' + suffix + '.service'
            subprocess.run(['/usr/bin/systemctl', '--user', 'stop', unit], env=c.system_env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=130)
            subprocess.run(['/usr/bin/systemctl', '--user', 'reset-failed', unit], env=c.system_env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def write_fixture(root):
    runtime = root / 'runtime'
    for name in ('controller.py', 'bootstrap_recovery.py'):
        dest = runtime / name
        dest.write_bytes((SCRIPTS / name).read_bytes())
        dest.chmod(0o600)
    fixture = runtime / 'bootstrap_fixture.py'
    fixture.write_text(
        'from __future__ import annotations\n'
        'import json\n'
        'import os\n'
        'from pathlib import Path\n'
        'import signal\n'
        'import sqlite3\n'
        'import subprocess\n'
        'import sys\n'
        'import time\n'
        '\n'
        'def _durable(path, value):\n'
        '    tmp = path.with_name(path.name + ".tmp")\n'
        '    tmp.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\\n")\n'
        '    os.chmod(tmp, 0o600)\n'
        '    os.replace(tmp, path)\n'
        '\n'
        'def _loads(path):\n'
        '    return json.loads(path.read_text())\n'
        '\n'
        'def _starttime(pid):\n'
        '    text = Path("/proc/%d/stat" % pid).read_text()\n'
        '    return text[text.rindex(")") + 2:].split()[19]\n'
        '\n'
        'def _show(unit):\n'
        '    output = subprocess.check_output(["/usr/bin/systemctl", "--user", "show", unit,\n'
        '                                      "-p", "ActiveState", "-p", "MainPID"])\n'
        '    return dict(line.split("=", 1) for line in output.decode().splitlines())\n'
        '\n'
        'def _start_unit(root):\n'
        '    unit = root.name + "-gateway.service"\n'
        '    run = subprocess.run(["/usr/bin/systemctl", "--user", "start", unit],\n'
        '                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
        '    if run.returncode != 0:\n'
        '        props = {"Type": "exec", "Restart": "no", "UMask": "0077",\n'
        '                 "KillMode": "control-group", "TimeoutStopSec": "3",\n'
        '                 "RuntimeMaxSec": "180", "WorkingDirectory": str(root / "runtime"),\n'
        '                 "StandardOutput": "null", "StandardError": "null"}\n'
        '        command = ["/usr/bin/systemd-run", "--user", "--unit=" + unit]\n'
        '        command += ["--property=" + k + "=" + v for k, v in props.items()]\n'
        '        command += [sys.executable, str(root / "runtime/bootstrap_fixture.py"), str(root), "serve"]\n'
        '        subprocess.check_call(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
        '    end = time.monotonic() + 5\n'
        '    while time.monotonic() < end:\n'
        '        try:\n'
        '            status = _show(unit)\n'
        '            if status["ActiveState"] == "active" and status["MainPID"] == (root / "run/service.pid").read_text().strip():\n'
        '                return\n'
        '        except Exception:\n'
        '            pass\n'
        '        time.sleep(0.05)\n'
        '    raise SystemExit(21)\n'
        '\n'
        'def _mode(root):\n'
        '    return "bootstrap" if (root / "dropins/90-bootstrap.conf").exists() else "legacy"\n'
        '\n'
        'def _source(mode):\n'
        '    return "candidate64cebba4f971db0fe3d8fbe294e23f6358aef541" if mode == "bootstrap" else "baseline9ccb53e3d15730fcae88b086ea954dfc377574aa"\n'
        '\n'
        'def _health(root, healthy=True):\n'
        '    pid = int((root / "run/service.pid").read_text())\n'
        '    argv = Path("/proc/%d/cmdline" % pid).read_bytes().decode().rstrip("\\0").split("\\0")\n'
        '    mode = _mode(root)\n'
        '    return {"healthy": healthy, "unit": root.name + "-gateway.service", "pid": pid,\n'
        '            "starttime": _starttime(pid), "argv": argv, "source": _source(mode),\n'
        '            "mode": mode, "observed": int(time.time())}\n'
        '\n'
        'def _record_db(root, mode):\n'
        '    conn = sqlite3.connect(root / "state.db")\n'
        '    try:\n'
        '        conn.execute("CREATE TABLE IF NOT EXISTS events (mode TEXT NOT NULL)")\n'
        '        conn.execute("INSERT INTO events(mode) VALUES (?)", (mode,))\n'
        '        conn.commit()\n'
        '    finally:\n'
        '        conn.close()\n'
        '\n'
        'def main():\n'
        '    root = Path(sys.argv[1]).resolve()\n'
        '    action = sys.argv[2]\n'
        '    fault = _loads(root / "run/fault.json")["fault"]\n'
        '    if action == "serve":\n'
        '        mode = _mode(root)\n'
        '        if mode == "bootstrap" and fault == "start-failure":\n'
        '            raise SystemExit(42)\n'
        '        (root / "run/service.pid").write_text(str(os.getpid()))\n'
        '        os.chmod(root / "run/service.pid", 0o600)\n'
        '        _record_db(root, mode)\n'
        '        _durable(root / "run/service-health.json", _health(root, healthy=True))\n'
        '        while True:\n'
        '            signal.pause()\n'
        '    if action == "reload":\n'
        '        if fault == "reload-failure" and (root / "dropins/90-bootstrap.conf").exists():\n'
        '            raise SystemExit(31)\n'
        '        print(json.dumps({"reloaded": True, "candidate_present": (root / "dropins/90-bootstrap.conf").exists()}))\n'
        '        return\n'
        '    if action == "drain":\n'
        '        pid = int((root / "run/service.pid").read_text())\n'
        '        if fault in ("drain-refusal", "pre-stop-supervisor-loss"):\n'
        '            _durable(root / "run/drain-marker.json", {"owned": True})\n'
        '        if fault == "drain-refusal":\n'
        '            raise SystemExit(32)\n'
        '        if fault == "pre-stop-supervisor-loss":\n'
        '            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])\n'
        '            _durable(root / "run/descendant.json", {"pid": child.pid})\n'
        '            os.kill(os.getppid(), signal.SIGKILL)\n'
        '            time.sleep(120)\n'
        '        print(json.dumps({"active_jobs": 0, "unit": root.name + "-gateway.service",\n'
        '                          "pid": pid, "starttime": _starttime(pid), "observed": int(time.time())}))\n'
        '        return\n'
        '    if action == "clear-drain":\n'
        '        marker = root / "run/drain-marker.json"\n'
        '        if marker.exists():\n'
        '            marker.unlink()\n'
        '            print(json.dumps({"cleared": True}))\n'
        '            return\n'
        '        print(json.dumps({"cleared": False, "reason": "fixture-no-marker"}))\n'
        '        return\n'
        '    if action in ("stop", "recover-stop"):\n'
        '        subprocess.check_call(["/usr/bin/systemctl", "--user", "stop", root.name + "-gateway.service"],\n'
        '                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
        '        return\n'
        '    if action in ("start", "recover-start"):\n'
        '        _start_unit(root)\n'
        '        return\n'
        '    if action in ("health", "recover-health"):\n'
        '        if action == "health" and fault == "changed-candidate":\n'
        '            (root / "dropins/90-bootstrap.conf").write_text("tampered\\n")\n'
        '            raise SystemExit(3)\n'
        '        if action == "health" and fault == "supervisor-loss":\n'
        '            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])\n'
        '            _durable(root / "run/descendant.json", {"pid": child.pid})\n'
        '            os.kill(os.getppid(), signal.SIGKILL)\n'
        '            time.sleep(120)\n'
        '        if action == "health" and fault == "deadline-timeout":\n'
        '            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])\n'
        '            _durable(root / "run/descendant.json", {"pid": child.pid})\n'
        '            time.sleep(120)\n'
        '        if action == "health" and fault in ("running-unhealthy", "recovery-health-failure"):\n'
        '            print(json.dumps(_health(root, healthy=False)))\n'
        '            raise SystemExit(4)\n'
        '        if action == "recover-health" and fault == "recovery-health-failure":\n'
        '            print(json.dumps(_health(root, healthy=False)))\n'
        '            raise SystemExit(5)\n'
        '        print(json.dumps(_health(root, healthy=True)))\n'
        '        return\n'
        '    raise SystemExit(2)\n'
        '\n'
        'if __name__ == "__main__":\n'
        '    main()\n'
    )
    fixture.chmod(0o600)
    return fixture


def systemd_run(unit, argv, root):
    props = {
        'Type': 'exec', 'Restart': 'no', 'UMask': '0077',
        'KillMode': 'control-group', 'TimeoutStopSec': '3',
        'RuntimeMaxSec': '180', 'WorkingDirectory': str(root / 'runtime'),
        'StandardOutput': 'null', 'StandardError': 'null',
    }
    command = ['/usr/bin/systemd-run', '--user', '--unit=' + unit]
    command += ['--property=' + k + '=' + v for k, v in props.items()]
    c.raw(command + argv, str(root), 10, c.system_env())


def start_gateway(root):
    unit = root.name + '-gateway.service'
    argv = [c.PYTHON, str(root / 'runtime/bootstrap_fixture.py'), str(root), 'serve']
    if c.show(unit)['ActiveState'] in ('inactive', 'failed'):
        c.raw(['/usr/bin/systemctl', '--user', 'start', unit], str(root), 10, c.system_env())
    else:
        systemd_run(unit, argv, root)
    wait_for(lambda: (root / 'run/service-health.json').exists()
             and c.show(unit)['ActiveState'] == 'active'
             and c.show(unit)['MainPID'] == (root / 'run/service.pid').read_text().strip())


def show_or_none(unit):
    try:
        return c.show(unit)
    except Exception:
        return None


def new_bootstrap_root(root, fault=''):
    os.umask(0o077)
    root = Path(root).absolute()
    root.mkdir(mode=0o700)
    for name in ('runtime', 'run', 'install', 'dropins'):
        (root / name).mkdir(mode=0o700)
    c.durable(root / 'sandbox.json', {'task': 't_e8912230', 'disposable': True}, exclusive=True)
    fixture = write_fixture(root)
    c.durable(root / 'run/fault.json', {'fault': fault}, exclusive=True)
    candidate = root / 'install/90-bootstrap.conf'
    candidate.write_text('[Service]\n# candidate bootstrap fixture\n')
    candidate.chmod(0o600)
    (root / 'sole-writer.lock').touch(mode=0o600)
    unit = root.name + '-gateway.service'
    systemd_run(unit, [c.PYTHON, str(fixture), str(root), 'serve'], root)
    wait_for(lambda: (root / 'run/service-health.json').exists())
    baseline = c.proc(int(c.show(unit)['MainPID']))
    baseline_health = json.loads((root / 'run/service-health.json').read_text())
    assert baseline_health['mode'] == 'legacy'
    now = int(time.time())
    lock_st = (root / 'sole-writer.lock').stat()
    command_timeout = 2 if fault == 'deadline-timeout' else 5
    reserve = 60 if fault == 'deadline-timeout' else 80
    packet = {
        'version': 1, 'command_timeout': command_timeout,
        'installation': str(root / 'runtime'), 'state': str(root / 'run'),
        'lock': str(root / 'sole-writer.lock'),
        'lock_identity': {'device': lock_st.st_dev, 'inode': lock_st.st_ino},
        'unit': unit, 'controller_unit': root.name + '-controller.service',
        'baseline_unit_sha256': c.digest(c.raw([c.SYSTEMCTL, '--user', 'cat', unit], '/', env=c.system_env()).encode()),
        'candidate_unit_sha256': c.digest(b'transient fixture has no rendered candidate unit\n'),
        'unit_definition_required': False,
        'baseline': baseline,
        'legacy': {'argv': baseline['argv'], 'source': 'baseline9ccb53e3d15730fcae88b086ea954dfc377574aa'},
        'bootstrap': {'argv': baseline['argv'], 'source': 'candidate64cebba4f971db0fe3d8fbe294e23f6358aef541'},
        'candidate': {'source': str(candidate), 'live': str(root / 'dropins/90-bootstrap.conf'),
                      'sha256': c.digest(candidate.read_bytes()), 'mode': 0o600},
        'checks': {},
        'window': {'start': now - 10, 'abort': now + 300, 'expiry': now + 560,
                   'recovery_deadline': now + 560, 'reserve': reserve},
        'commands': [], 'recovery': [], 'artifacts': [],
    }
    for name in ('reload', 'drain', 'stop', 'clear-drain', 'start', 'health',
                 'recover-stop', 'recover-start', 'recover-health'):
        action = name
        packet['checks'][name] = {'id': name, 'kind': 'command',
                                  'argv': [c.PYTHON, str(fixture), str(root), action],
                                  'cwd': str(root / 'runtime'), 'env': c.system_env(),
                                  'timeout': command_timeout}
    packet['commands'], packet['recovery'] = b.command_plan(packet)
    artifact_paths = {str(root / 'runtime/controller.py'), str(root / 'runtime/bootstrap_recovery.py'),
                      str(fixture), str(candidate), c.PYTHON, c.SYSTEMCTL, '/usr/bin/systemd-run'}
    for command in [*packet['commands'], *packet['recovery']]:
        if command['kind'] == 'command':
            artifact_paths.update(c.command_files(command['argv']))
    packet['artifacts'] = []
    for path in sorted(artifact_paths):
        st = Path(path).stat()
        packet['artifacts'].append({'path': path, 'sha256': c.digest(Path(path).read_bytes()),
                                    'uid': c.artifact_uid(path), 'mode': st.st_mode & 0o777})
    c.durable(root / 'run/packet.json', packet, exclusive=True)
    authority = c.digest((root / 'run/packet.json').read_bytes())
    return root, packet, authority


def cli(root, authority, *options):
    return subprocess.run([c.PYTHON, str(root / 'runtime/bootstrap_recovery.py'),
                           str(root / 'run/packet.json'), '--authority', authority, *options],
                          capture_output=True, text=True, env=c.system_env(), timeout=20)


def result(root):
    path = root / 'run/result.json'
    return json.loads(path.read_text()) if path.exists() else None


def finished(root, timeout=45):
    def done():
        status = show_or_none(root.name + '-controller.service')
        if status and status['ActiveState'] in ('inactive', 'failed') and (root / 'run/result.json').exists():
            return result(root)
        return None
    return wait_for(done, timeout)


def db_modes(root):
    conn = sqlite3.connect(root / 'state.db')
    try:
        return [row[0] for row in conn.execute('SELECT mode FROM events ORDER BY rowid')]
    finally:
        conn.close()


def journal_rows(root):
    return [json.loads(line) for line in (root / 'run/journal.jsonl').read_text().splitlines()]


def completed(argv, returncode=0, stdout='', stderr=''):
    return subprocess.CompletedProcess(argv, returncode, stdout.encode(), stderr.encode())


def base_template_input(tmp_path):
    home = tmp_path / 'home'
    state = tmp_path / 'state'
    install = SCRIPTS
    repo = tmp_path / 'repo'
    for path in (home, state, repo):
        path.mkdir(mode=0o700)
    hold = state / 'hold.json'
    hold.write_text('{}\n')
    hold.chmod(0o600)
    candidate = tmp_path / 'candidate.conf'
    candidate.write_text('[Service]\n')
    candidate.chmod(0o600)
    lock = tmp_path / 'sole-writer.lock'
    lock.touch(mode=0o600)
    lock_st = lock.stat()
    now = int(time.time())
    return {
        'version': 1, 'command_timeout': 7,
        'installation': str(install), 'state': str(state),
        'lock': str(lock),
        'lock_identity': {'device': lock_st.st_dev, 'inode': lock_st.st_ino},
        'unit': 'hermes-gateway.service', 'controller_unit': 'ang-bootstrap.service',
        'baseline_unit_sha256': '0' * 64, 'candidate_unit_sha256': '1' * 64,
        'unit_definition_required': True,
        'baseline': {'pid': 123, 'starttime': '456', 'cgroup': '/user.slice/test.scope',
                     'argv': [c.PYTHON, 'legacy']},
        'legacy': {'argv': [c.PYTHON, 'legacy'], 'source': 'legacy9ccb53e3d15730fcae88b086ea954dfc377574aa'},
        'bootstrap': {'argv': [c.PYTHON, 'bootstrap'], 'source': 'qualified64cebba4f971db0fe3d8fbe294e23f6358aef541'},
        'candidate': {'source': str(candidate), 'live': str(tmp_path / 'dropins/90-bootstrap.conf'),
                      'sha256': c.digest(candidate.read_bytes()), 'mode': 0o600},
        'checks': {'drain': {'id': 'drain', 'kind': 'command', 'argv': ['/usr/bin/true'],
                             'cwd': '/', 'env': c.system_env(), 'timeout': 1},
                   'health': {'id': 'health', 'kind': 'command', 'argv': ['/usr/bin/true'],
                              'cwd': '/', 'env': c.system_env(), 'timeout': 1},
                   'recover-health': {'id': 'recover-health', 'kind': 'command',
                                      'argv': ['/usr/bin/true'], 'cwd': '/',
                                      'env': c.system_env(), 'timeout': 1}},
        'bootstrap_bindings': {'hermes_home': str(home), 'hold_json': str(hold),
                               'repo': str(repo), 'startup_json': str(state / 'startup.json'),
                               'live_json': str(state / 'runtime-health.json'),
                               'max_age': 44, 'drain_timeout': 8,
                               'health_timeout': 123, 'startup_timeout': 91},
        'window': {'start': now - 10, 'abort': now + 300, 'expiry': now + 620,
                   'recovery_deadline': now + 620, 'reserve': 80},
        'commands': [], 'recovery': [], 'artifacts': [],
    }


def test_template_binds_production_bootstrap_commands(tmp_path):
    packet = template.build_packet(base_template_input(tmp_path))
    assert 'bootstrap_bindings' not in packet
    assert packet['checks']['drain']['argv'][:3] == [
        c.PYTHON, str(SCRIPTS / 'bootstrap_runtime_bindings.py'), 'legacy-drain']
    assert packet['checks']['drain']['argv'][6:10] == [
        'hermes-gateway.service', '123', '456', '--recovery-deadline']
    drain_argv = packet['checks']['drain']['argv']
    assert drain_argv[drain_argv.index('--max-age'):drain_argv.index('--max-age') + 2] == [
        '--max-age', '44']
    assert packet['checks']['drain']['timeout'] == 8
    assert packet['checks']['clear-drain']['argv'] == [
        c.PYTHON, str(SCRIPTS / 'bootstrap_runtime_bindings.py'), 'clear-drain',
        str(tmp_path / 'home')]
    assert packet['checks']['health']['argv'][:2] == [c.PYTHON, str(SCRIPTS / 'runtime_health.py')]
    assert packet['checks']['health']['argv'][-4:] == ['--max-age', '90', '--startup-timeout', '91']
    assert packet['checks']['health']['argv'][-2:] == ['--startup-timeout', '91']
    assert packet['checks']['health']['timeout'] == 123
    assert packet['checks']['recover-health']['argv'][:3] == [
        c.PYTHON, str(SCRIPTS / 'bootstrap_runtime_bindings.py'), 'legacy-health']
    assert json.loads(packet['checks']['recover-health']['argv'][5]) == [c.PYTHON, 'legacy']
    assert packet['checks']['recover-health']['argv'][-4:] == [
        '--max-age', '44', '--startup-timeout', '123']
    assert packet['checks']['recover-health']['timeout'] == 123
    assert (packet['commands'], packet['recovery']) == b.command_plan(packet)
    artifact_paths = {entry['path'] for entry in packet['artifacts']}
    assert {str(SCRIPTS / name) for name in (
        'bootstrap_runtime_bindings.py', 'runtime_health.py', 'drain_proof.py', 'health.py'
    )} <= artifact_paths


def test_template_aligns_recover_health_startup_timeout_above_runtime_bound(tmp_path):
    data = base_template_input(tmp_path)
    data['bootstrap_bindings']['health_timeout'] = 241
    packet = template.build_packet(data)
    assert packet['checks']['health']['argv'][-2:] == ['--startup-timeout', '91']
    assert packet['checks']['recover-health']['argv'][-2:] == ['--startup-timeout', '241']
    assert packet['checks']['recover-health']['timeout'] == 241


def test_template_rejects_unbounded_freshness(tmp_path):
    data = base_template_input(tmp_path)
    data['bootstrap_bindings']['max_age'] = 999999
    with pytest.raises(c.Refusal, match='bootstrap max_age out of bounds'):
        template.build_packet(data)


def write_hold(path, *, repo, unit, pid, starttime, recovery_deadline):
    hold = {
        'kind': 'release-admission-hold', 'repo': str(repo), 'unit': unit,
        'pid': pid, 'starttime': starttime, 'observed': int(time.time()),
        'valid_until': recovery_deadline + 30,
        'recovery_deadline': recovery_deadline,
        'owner': 'fixture-owner', 'recovery_owner': 'fixture-recovery',
        'approved': True,
        'coverage': {key: 'fixture verified ' + key for key in bindings.COVERAGE},
    }
    c.durable(path, hold, exclusive=True)


def serve_status_socket(home, payloads):
    socket_path = home / 'gateway.sock'
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen()
    done = threading.Event()

    def run():
        try:
            for payload in payloads:
                conn, _ = server.accept()
                with conn:
                    conn.recv(65536)
                    conn.sendall(json.dumps({'ok': True, 'result': payload}).encode() + b'\n')
        finally:
            done.set()
            server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return done, thread


def test_legacy_drain_producer_writes_marker_and_requires_draining_zero(tmp_path):
    from gateway import drain_control

    home = tmp_path / 'home'
    repo = tmp_path / 'repo'
    for path in (home, repo):
        path.mkdir(mode=0o700)
    pid, starttime, unit = 321, '654', 'hermes-gateway.service'
    deadline = int(time.time()) + 120
    hold = tmp_path / 'hold.json'
    write_hold(hold, repo=repo, unit=unit, pid=pid, starttime=starttime,
               recovery_deadline=deadline)
    status = {'pid': pid, 'answering_pid': pid, 'start_time': starttime,
              'gateway_state': 'draining', 'active_agents': 0}
    done, thread = serve_status_socket(home, [status, status])
    proof = bindings.prove_legacy_drain(home, hold, str(repo), unit, pid, starttime,
                                        max_age=30, repeat_delay=0.01,
                                        recovery_deadline=deadline, observe_timeout=2)
    assert proof['active_jobs'] == 0
    assert proof['unit'] == unit
    assert json.loads((home / '.drain_request.json').read_text())['principal'] == 'release-bootstrap'
    assert drain_control.drain_requested(home=home) is True
    clear = bindings.clear_legacy_drain_request(home)
    assert clear['cleared'] is True
    assert drain_control.drain_requested(home=home) is False
    assert done.wait(2)
    thread.join(2)


def test_legacy_drain_clear_refuses_unowned_marker(tmp_path):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    marker = home / '.drain_request.json'
    marker.write_text(json.dumps({'action': 'drain', 'principal': 'operator'}) + '\n')
    marker.chmod(0o600)
    with pytest.raises(c.Refusal, match='drain marker not owned'):
        bindings.clear_legacy_drain_request(home)
    assert marker.exists()


@pytest.mark.parametrize('status', [
    {'pid': 321, 'answering_pid': 321, 'start_time': '654',
     'gateway_state': 'draining', 'active_agents': 1},
    {'pid': 321, 'answering_pid': 321, 'start_time': '654',
     'gateway_state': 'draining'},
])
def test_legacy_drain_producer_refuses_busy_or_incomplete_status(tmp_path, monkeypatch, status):
    home = tmp_path / 'home'
    repo = tmp_path / 'repo'
    for path in (home, repo):
        path.mkdir(mode=0o700)
    pid, starttime, unit = 321, '654', 'hermes-gateway.service'
    deadline = int(time.time()) + 120
    hold = tmp_path / 'hold.json'
    write_hold(hold, repo=repo, unit=unit, pid=pid, starttime=starttime,
               recovery_deadline=deadline)
    monkeypatch.setattr(bindings, '_query_socket', lambda home, verb: status)
    with pytest.raises(c.Refusal, match='legacy drain observation missing|active work count missing'):
        bindings.prove_legacy_drain(home, hold, str(repo), unit, pid, starttime,
                                    max_age=30, repeat_delay=0.01,
                                    recovery_deadline=deadline, observe_timeout=0.01)


def test_legacy_health_producer_uses_systemd_proc_and_live_status(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    actual = {'pid': 789, 'starttime': '987', 'cgroup': '/user.slice/test.scope',
              'argv': [c.PYTHON, 'legacy']}
    monkeypatch.setattr(bindings, 'show',
                        lambda unit: {'ActiveState': 'active', 'MainPID': str(actual['pid'])})
    monkeypatch.setattr(bindings, 'proc', lambda pid: actual)

    def query(_home, verb):
        if verb == 'identify':
            return {'pid': actual['pid'], 'start_time': actual['starttime']}
        return {'pid': actual['pid'], 'answering_pid': actual['pid'],
                'start_time': actual['starttime'], 'gateway_state': 'running',
                'answered_at': time.time()}

    monkeypatch.setattr(bindings, '_query_socket', query)
    proof = bindings.prove_legacy_health(home, 'hermes-gateway.service', actual['argv'],
                                         'legacy9ccb53e3d15730fcae88b086ea954dfc377574aa',
                                         'legacy', max_age=30)
    assert proof['healthy'] is True
    assert proof['argv'] == actual['argv']
    assert proof['source'] == 'legacy9ccb53e3d15730fcae88b086ea954dfc377574aa'


def test_legacy_health_retries_delayed_startup_socket_readiness(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    actual = {'pid': 789, 'starttime': '987', 'cgroup': '/user.slice/test.scope',
              'argv': [c.PYTHON, 'legacy']}
    monkeypatch.setattr(bindings, 'show',
                        lambda unit: {'ActiveState': 'active', 'MainPID': str(actual['pid'])})
    monkeypatch.setattr(bindings, 'proc', lambda pid: actual)
    attempts = {'count': 0}

    def query(_home, verb):
        if verb == 'identify':
            attempts['count'] += 1
            if attempts['count'] < 3:
                raise RuntimeError('gateway control socket missing')
            return {'pid': actual['pid'], 'start_time': actual['starttime']}
        return {'pid': actual['pid'], 'answering_pid': actual['pid'],
                'start_time': actual['starttime'], 'gateway_state': 'running',
                'answered_at': time.time()}

    monkeypatch.setattr(bindings, '_query_socket', query)
    monkeypatch.setattr(bindings.time, 'sleep', lambda delay: None)
    proof = bindings.prove_legacy_health(home, 'hermes-gateway.service', actual['argv'],
                                         'legacy9ccb53e3d15730fcae88b086ea954dfc377574aa',
                                         'legacy', max_age=30, startup_timeout=2)
    assert proof['healthy'] is True
    assert attempts['count'] == 3


def test_legacy_health_does_not_retry_wrong_identity(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    actual = {'pid': 789, 'starttime': '987', 'cgroup': '/user.slice/test.scope',
              'argv': [c.PYTHON, 'legacy']}
    monkeypatch.setattr(bindings, 'show',
                        lambda unit: {'ActiveState': 'active', 'MainPID': str(actual['pid'])})
    monkeypatch.setattr(bindings, 'proc', lambda pid: actual)
    calls = {'count': 0}

    def query(_home, verb):
        calls['count'] += 1
        if verb == 'identify':
            return {'pid': actual['pid'] + 1, 'start_time': actual['starttime']}
        return {'pid': actual['pid'] + 1, 'answering_pid': actual['pid'] + 1,
                'start_time': actual['starttime'], 'gateway_state': 'running',
                'answered_at': time.time()}

    monkeypatch.setattr(bindings, '_query_socket', query)
    monkeypatch.setattr(bindings.time, 'sleep',
                        lambda delay: (_ for _ in ()).throw(AssertionError('unexpected retry')))
    with pytest.raises(c.Refusal, match='gateway process mismatch'):
        bindings.prove_legacy_health(home, 'hermes-gateway.service', actual['argv'],
                                     'legacy9ccb53e3d15730fcae88b086ea954dfc377574aa',
                                     'legacy', max_age=30, startup_timeout=2)
    assert calls['count'] == 2


def test_legacy_health_refuses_permanent_unhealthy_gateway(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    actual = {'pid': 789, 'starttime': '987', 'cgroup': '/user.slice/test.scope',
              'argv': [c.PYTHON, 'legacy']}
    monkeypatch.setattr(bindings, 'show',
                        lambda unit: {'ActiveState': 'active', 'MainPID': str(actual['pid'])})
    monkeypatch.setattr(bindings, 'proc', lambda pid: actual)

    def query(_home, verb):
        if verb == 'identify':
            return {'pid': actual['pid'], 'start_time': actual['starttime']}
        return {'pid': actual['pid'], 'answering_pid': actual['pid'],
                'start_time': actual['starttime'], 'gateway_state': 'starting',
                'answered_at': time.time()}

    monotonic = iter([0.0, 0.05, 0.06, 0.2])
    monkeypatch.setattr(bindings, '_query_socket', query)
    monkeypatch.setattr(bindings.time, 'monotonic', lambda: next(monotonic))
    monkeypatch.setattr(bindings.time, 'sleep', lambda delay: None)
    with pytest.raises(bindings.TransientStartupReadiness, match='gateway not running'):
        bindings.prove_legacy_health(home, 'hermes-gateway.service', actual['argv'],
                                     'legacy9ccb53e3d15730fcae88b086ea954dfc377574aa',
                                     'legacy', max_age=30, startup_timeout=0.1)


def write_required_definition_packet(tmp_path, candidate_definition='candidate unit definition\n'):
    root = (tmp_path / (PREFIX + uuid.uuid4().hex[:12])).absolute()
    root.mkdir(mode=0o700)
    for name in ('runtime', 'run', 'install', 'dropins'):
        (root / name).mkdir(mode=0o700)
    baseline_definition = 'baseline unit definition\n'
    (root / 'run/baseline-definition.txt').write_text(baseline_definition)
    (root / 'run/current-definition.txt').write_text(candidate_definition)
    for path in (root / 'run/baseline-definition.txt', root / 'run/current-definition.txt'):
        path.chmod(0o600)
    fake_systemctl = root / 'runtime/systemctl-fixture.py'
    fake_systemctl.write_text(
        '#!/usr/bin/python3\n'
        'from pathlib import Path\n'
        'import sys\n'
        f'root = Path({str(root)!r})\n'
        'if sys.argv[1:3] != ["--user", "cat"]:\n'
        '    raise SystemExit(2)\n'
        'definition = "current-definition.txt" if (root / "dropins/90-bootstrap.conf").exists() else "baseline-definition.txt"\n'
        'sys.stdout.write((root / "run" / definition).read_text())\n'
    )
    fake_systemctl.chmod(0o700)
    candidate = root / 'install/90-bootstrap.conf'
    candidate.write_text('[Service]\n# candidate bootstrap fixture\n')
    candidate.chmod(0o600)
    lock = root / 'sole-writer.lock'
    lock.touch(mode=0o600)
    lock_st = lock.stat()
    now = int(time.time())
    baseline = {'pid': os.getpid(), 'starttime': '1', 'cgroup': '/user.slice/test.scope',
                'argv': [sys.executable, 'legacy']}
    packet = {
        'version': 1, 'command_timeout': 1,
        'installation': str(root / 'runtime'), 'state': str(root / 'run'),
        'lock': str(lock),
        'lock_identity': {'device': lock_st.st_dev, 'inode': lock_st.st_ino},
        'unit': root.name + '-gateway.service',
        'controller_unit': root.name + '-controller.service',
        'baseline_unit_sha256': c.digest(baseline_definition.strip().encode()),
        'candidate_unit_sha256': c.digest(candidate_definition.strip().encode()),
        'unit_definition_required': True,
        'baseline': baseline,
        'legacy': {'argv': baseline['argv'], 'source': 'baseline'},
        'bootstrap': {'argv': baseline['argv'], 'source': 'candidate'},
        'candidate': {'source': str(candidate), 'live': str(root / 'dropins/90-bootstrap.conf'),
                      'sha256': c.digest(candidate.read_bytes()), 'mode': 0o600},
        'checks': {},
        'window': {'start': now - 10, 'abort': now + 300, 'expiry': now + 560,
                   'recovery_deadline': now + 560, 'reserve': 80},
        'commands': [], 'recovery': [], 'artifacts': [],
    }
    true = '/usr/bin/true'
    for name in ('reload', 'drain', 'stop', 'clear-drain', 'start', 'health',
                 'recover-stop', 'recover-start', 'recover-health'):
        packet['checks'][name] = {'id': name, 'kind': 'command', 'argv': [true],
                                  'cwd': str(root / 'runtime'), 'env': c.system_env(),
                                  'timeout': 1}
    packet['commands'], packet['recovery'] = b.command_plan(packet)
    artifact_paths = {str(Path(b.__file__).resolve()), str(Path(b.__file__).with_name('controller.py')),
                      b.PYTHON, str(fake_systemctl), '/usr/bin/systemd-run', str(candidate), true}
    packet['artifacts'] = []
    for path in sorted(artifact_paths):
        st = Path(path).stat()
        packet['artifacts'].append({'path': path, 'sha256': c.digest(Path(path).read_bytes()),
                                    'uid': c.artifact_uid(path), 'mode': st.st_mode & 0o777})
    packet_path = root / 'run/packet.json'
    c.durable(packet_path, packet, exclusive=True)
    return root, packet_path, c.digest(packet_path.read_bytes()), str(fake_systemctl), baseline


def required_definition_run(tmp_path, monkeypatch, candidate_definition='candidate unit definition\n'):
    root, packet_path, authority, fake_systemctl, baseline = write_required_definition_packet(
        tmp_path, candidate_definition)
    monkeypatch.setattr(b, 'SYSTEMCTL', fake_systemctl)
    monkeypatch.setattr(b, 'show', lambda unit: {'ActiveState': 'active', 'MainPID': str(baseline['pid'])})
    monkeypatch.setattr(b, 'proc', lambda pid: baseline)
    return root, b.BootstrapRun(packet_path, authority)


def test_unit_definition_required_accepts_candidate_phase(tmp_path, monkeypatch):
    root, run = required_definition_run(tmp_path, monkeypatch)
    run.baseline_active()
    run.install_candidate()
    run.baseline_process_active()
    run.candidate_unit_definition()
    assert (root / 'dropins/90-bootstrap.conf').read_bytes() == (root / 'install/90-bootstrap.conf').read_bytes()


def test_unit_definition_required_rejects_candidate_drift(tmp_path, monkeypatch):
    root, run = required_definition_run(tmp_path, monkeypatch)
    run.baseline_active()
    run.install_candidate()
    (root / 'run/current-definition.txt').write_text('unexpected unit definition\n')
    (root / 'run/current-definition.txt').chmod(0o600)
    with pytest.raises(b.Refusal, match='candidate service definition drift'):
        run.candidate_unit_definition()


def manager_reload_run(tmp_path):
    root = tmp_path / 'reload-precheck'
    root.mkdir(mode=0o700)
    run = object.__new__(b.BootstrapRun)
    run.state = root
    run.p = {'unit': 'target.service', 'controller_unit': 'controller.service'}
    return run


def test_manager_reload_precheck_refuses_missing_load_state(tmp_path, monkeypatch):
    run = manager_reload_run(tmp_path)

    def fake_run(argv, **kwargs):
        assert argv[2] == 'list-units'
        return completed(argv, stdout='ready.service loaded active running Ready\n')

    monkeypatch.setattr(b.subprocess, 'run', fake_run)
    monkeypatch.setattr(b, 'systemctl_show_props',
                        lambda unit, props: {'_returncode': '0', 'NeedDaemonReload': 'no'})
    with pytest.raises(b.Refusal, match='LoadState missing for loaded unit ready.service'):
        run.manager_reload_safe()


def test_manager_reload_precheck_refuses_pending_unrelated_loaded_unit(tmp_path, monkeypatch):
    run = manager_reload_run(tmp_path)

    def fake_run(argv, **kwargs):
        return completed(argv, stdout='target.service loaded active running Target\n'
                                      'needs-reload.service loaded active running Other\n')

    monkeypatch.setattr(b.subprocess, 'run', fake_run)

    def fake_show(unit, props):
        assert unit == 'needs-reload.service'
        return {'_returncode': '0', 'LoadState': 'loaded', 'NeedDaemonReload': 'yes'}

    monkeypatch.setattr(b, 'systemctl_show_props', fake_show)
    with pytest.raises(b.Refusal, match='unrelated loaded units need reload: needs-reload.service'):
        run.manager_reload_safe()


def test_pre_stop_recovery_retry_continues_after_candidate_removed(tmp_path):
    root = tmp_path / 'retry-pre-stop'
    root.mkdir(mode=0o700)
    live = root / '90-bootstrap.conf'
    journal = root / 'journal.jsonl'
    journal.write_text(
        json.dumps({'event': 'candidate-removed'}) + '\n'
        + json.dumps({'event': 'intent', 'op': 'drain'}) + '\n'
    )
    journal.chmod(0o600)
    run = object.__new__(b.BootstrapRun)
    run.state = root
    run.authority = 'a' * 64
    run.p = {
        'candidate': {'live': str(live), 'sha256': 'b' * 64},
        'unit_definition_required': True,
        'baseline_unit_sha256': 'c' * 64,
        'checks': {},
    }
    for name in ('reload', 'clear-drain', 'recover-health'):
        run.p['checks'][name] = {'id': name, 'kind': 'command', 'argv': ['/usr/bin/true'],
                                 'cwd': '/', 'env': c.system_env(), 'timeout': 1}
    calls = []
    results = []
    run.baseline_process_active = lambda: calls.append('baseline')
    run.validate_unit_definition = lambda expected, reason: calls.append(('definition', expected, reason))
    run.run_recovery_command_while_baseline_active = lambda command: calls.append(command['id'])
    run.result = lambda status, reason: results.append((status, reason))

    run.recover_before_stop()

    assert 'reload' in calls
    assert 'clear-drain' in calls
    assert 'recover-health' in calls
    assert results == [('preflight-blocked',
                        'bootstrap candidate removed before stop; legacy left running')]


@pytest.mark.linux_only
def test_same_service_bootstrap_success(bootstrap_sandbox):
    root, packet, authority = bootstrap_sandbox()
    baseline_pid = packet['baseline']['pid']
    preflight = cli(root, authority)
    assert preflight.returncode == 0, preflight.stderr
    applied = cli(root, authority, '--apply')
    assert applied.returncode == 0, applied.stderr
    outcome = finished(root)
    assert outcome['status'] == 'bootstrap-active', outcome
    assert c.show(packet['unit'])['ActiveState'] == 'active'
    assert c.show(packet['unit'])['MainPID'] != str(baseline_pid)
    assert (root / 'dropins/90-bootstrap.conf').read_bytes() == (root / 'install/90-bootstrap.conf').read_bytes()
    assert json.loads((root / 'run/bootstrap-health.json').read_text())['mode'] == 'bootstrap'
    assert db_modes(root) == ['legacy', 'bootstrap']


@pytest.mark.linux_only
@pytest.mark.parametrize('fault', ['start-failure', 'running-unhealthy'])
def test_bootstrap_inverse_recovers_failed_start_and_unhealthy(bootstrap_sandbox, fault):
    root, packet, authority = bootstrap_sandbox(fault)
    applied = cli(root, authority, '--apply')
    assert applied.returncode == 0, applied.stderr
    outcome = finished(root)
    assert outcome['status'] == 'legacy-recovered', outcome
    assert not (root / 'dropins/90-bootstrap.conf').exists()
    assert c.show(packet['unit'])['ActiveState'] == 'active'
    assert json.loads((root / 'run/legacy-health.json').read_text())['mode'] == 'legacy'
    if fault == 'running-unhealthy':
        assert 'bootstrap' in db_modes(root)  # candidate write survived; no DB restore occurred.
    rows = [json.loads(line) for line in (root / 'run/journal.jsonl').read_text().splitlines()]
    events = [row['event'] for row in rows]
    assert events.index('quiescent') < events.index('candidate-removed')
    assert any(row['event'] == 'recovery-begin' and row['model_used'] is False for row in rows)


@pytest.mark.linux_only
def test_bootstrap_exact_byte_refusal_fail_closed(bootstrap_sandbox):
    root, packet, authority = bootstrap_sandbox('changed-candidate')
    assert cli(root, authority, '--apply').returncode == 0
    outcome = finished(root)
    assert outcome['status'] == 'recovery-required', outcome
    assert outcome['reason'] == 'candidate live bytes changed'
    assert (root / 'dropins/90-bootstrap.conf').read_text() == 'tampered\n'
    assert c.show(packet['unit'])['ActiveState'] in ('inactive', 'failed')


@pytest.mark.linux_only
def test_bootstrap_install_open_failure_does_not_stop_legacy(bootstrap_sandbox):
    root, packet, authority = bootstrap_sandbox()
    temp = Path(packet['candidate']['live']).with_name(Path(packet['candidate']['live']).name + '.tmp')
    temp.write_text('stale temp blocks exclusive install\n')
    temp.chmod(0o600)
    assert cli(root, authority, '--apply').returncode == 0
    outcome = finished(root)
    assert outcome['status'] == 'preflight-blocked', outcome
    assert outcome['reason'] == 'bootstrap candidate was not installed; legacy left running'
    assert not Path(packet['candidate']['live']).exists()
    assert c.show(packet['unit'])['MainPID'] == str(packet['baseline']['pid'])
    rows = [json.loads(line) for line in (root / 'run/journal.jsonl').read_text().splitlines()]
    assert not any(row.get('op') == 'stop' for row in rows)


@pytest.mark.linux_only
@pytest.mark.parametrize('fault', ['reload-failure', 'drain-refusal', 'pre-stop-supervisor-loss'])
def test_bootstrap_pre_stop_recovery_does_not_stop_legacy(bootstrap_sandbox, fault):
    root, packet, authority = bootstrap_sandbox(fault)
    assert cli(root, authority, '--apply').returncode == 0
    outcome = finished(root, 60)
    assert outcome['status'] == 'preflight-blocked', outcome
    assert outcome['reason'] == 'bootstrap candidate removed before stop; legacy left running'
    assert not (root / 'dropins/90-bootstrap.conf').exists()
    assert c.show(packet['unit'])['ActiveState'] == 'active'
    assert c.show(packet['unit'])['MainPID'] == str(packet['baseline']['pid'])
    assert db_modes(root) == ['legacy']
    rows = journal_rows(root)
    ops = [row.get('op') for row in rows if row['event'] == 'intent']
    assert 'stop' not in ops
    assert 'recover-stop' not in ops
    assert any(row['event'] == 'recovery-begin' and row.get('pre_stop') is True for row in rows)
    if fault in ('drain-refusal', 'pre-stop-supervisor-loss'):
        assert 'clear-drain' in ops
        assert not (root / 'run/drain-marker.json').exists()
    if fault == 'pre-stop-supervisor-loss':
        assert any(row['event'] == 'fenced' for row in rows)


@pytest.mark.linux_only
def test_bootstrap_stale_stop_intent_blocks_before_mutation(bootstrap_sandbox):
    root, packet, authority = bootstrap_sandbox()
    c.durable(root / 'run/stop-intent.json', {'authority': '0' * 64}, exclusive=True)
    check = cli(root, authority, '--apply')
    assert check.returncode != 0
    outcome = result(root)
    assert outcome['status'] == 'preflight-blocked', outcome
    assert outcome['reason'] == 'stale intent marker'
    assert not Path(packet['candidate']['live']).exists()
    assert not (root / 'run/mutation-intent.json').exists()
    assert c.show(packet['unit'])['MainPID'] == str(packet['baseline']['pid'])


@pytest.mark.linux_only
@pytest.mark.parametrize('fault', ['supervisor-loss', 'deadline-timeout'])
def test_bootstrap_guard_fences_controller_descendants_and_recovers(bootstrap_sandbox, fault):
    root, packet, authority = bootstrap_sandbox(fault)
    assert cli(root, authority, '--apply').returncode == 0
    outcome = finished(root, 60)
    assert outcome['status'] == 'legacy-recovered', outcome
    rows = [json.loads(line) for line in (root / 'run/journal.jsonl').read_text().splitlines()]
    assert any(row['event'] == 'fenced' for row in rows)
    locks = [row['inode'] for row in rows if row['event'] == 'lock-acquired']
    assert len(set(locks)) == 1 and len(locks) >= 2
    descendant = json.loads((root / 'run/descendant.json').read_text())['pid']
    controller_group = c.show(packet['controller_unit'])['ControlGroup'] or packet['baseline']['cgroup']
    assert descendant not in c.cg_pids(controller_group)
    assert not (root / 'dropins/90-bootstrap.conf').exists()


@pytest.mark.linux_only
def test_bootstrap_recovery_health_failure_truthful(bootstrap_sandbox):
    root, packet, authority = bootstrap_sandbox('recovery-health-failure')
    assert cli(root, authority, '--apply').returncode == 0
    outcome = finished(root)
    assert outcome['status'] == 'recovery-required', outcome
    assert 'command failed' in outcome['reason']
    assert not (root / 'dropins/90-bootstrap.conf').exists()
    assert '--inspect-recovery' in outcome['operator_command']


@pytest.mark.linux_only
def test_bootstrap_preflight_blocks_without_mutation(bootstrap_sandbox):
    root, packet, authority = bootstrap_sandbox()
    Path(packet['candidate']['live']).write_text('preexisting unknown bytes\n')
    Path(packet['candidate']['live']).chmod(0o600)
    check = cli(root, authority, '--apply')
    assert check.returncode != 0
    assert result(root)['status'] == 'preflight-blocked'
    assert (root / 'dropins/90-bootstrap.conf').read_text() == 'preexisting unknown bytes\n'
    assert not (root / 'run/mutation-intent.json').exists()
    assert c.show(packet['unit'])['MainPID'] == str(packet['baseline']['pid'])
