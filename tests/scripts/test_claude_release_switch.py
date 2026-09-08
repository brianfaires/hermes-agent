"""Real disposable Git/systemd; simulated application health and deterministic model.

No real services, network credentials, model billing or repository imports required.
"""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import socket

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts' / 'claude_release_switch'
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'claude_release_switch'
TEST_PYTHON = Path('/usr/bin/python3')
sys.path.insert(0, str(SCRIPTS))
import controller as c
import fixture as f
import launch_gateway as launcher
sys.path.remove(str(SCRIPTS))


def wait_for(check, timeout=35):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError('bounded observable condition not reached')


def _module_socket_status(home):
    socket_path = home / 'gateway.sock'
    if not socket_path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(str(socket_path))
            client.sendall(b'{"verb":"status","id":1,"protocol":1}\n')
            raw = client.recv(512 * 1024)
        response = json.loads(raw.splitlines()[0].decode())
    except Exception:
        return None
    return response.get('result') if response.get('ok') is True else None


@pytest.fixture
def sandbox(tmp_path):
    try:
        c.raw(['/usr/bin/systemctl', '--user', 'show', '--property=Version'], '/', env=c.system_env())
    except Exception:
        pytest.skip('real user-systemd unavailable; production topology not qualified')
    roots = []
    def create(operation='stage', fault_point='', fault_kind=''):
        root = tmp_path / (f.PREFIX + uuid.uuid4().hex[:12])
        roots.append(root)
        p, authority = f.new(root, operation, fault_point=fault_point, fault_kind=fault_kind)
        return root, p, authority
    yield create
    for root in roots:
        if (root / 'sandbox.json').exists():
            f.lifecycle(root, 'cleanup')


def cli(root, authority, *options):
    return subprocess.run([c.PYTHON, str(root / 'runtime/controller.py'), str(root / 'run/packet.json'),
                           '--authority', authority, *options], capture_output=True, text=True,
                          env=c.system_env(), timeout=20)


def result(root):
    path = root / 'run/result.json'
    return json.loads(path.read_text()) if path.exists() else None


def finished(root, timeout=35):
    def done():
        status = c.show(root.name + '-controller.service')
        return result(root) if status['ActiveState'] in ('inactive', 'failed') else None
    return wait_for(done, timeout)


def module_service_runtime(root):
    runtime = root / 'runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    for name in ('controller.py', 'health.py', 'wire_launcher.py', 'runtime_health.py', 'drain_proof.py'):
        dest = runtime / name
        dest.write_bytes(((FIXTURES if name == 'wire_launcher.py' else SCRIPTS) / name).read_bytes())
        dest.chmod(0o600)
    (runtime / 'lifecycle.py').write_text(
        '#!/usr/bin/python3\n'
        'from __future__ import annotations\n'
        'import json\n'
        'import sys\n'
        'import time\n'
        'from pathlib import Path\n'
        '\n'
        'from controller import durable, proc, show\n'
        'from drain_proof import COVERAGE, prove as prove_drain\n'
        'from runtime_health import collect as collect_health\n'
        '\n'
        'def main():\n'
        '    root = Path(sys.argv[1]).resolve()\n'
        '    action = sys.argv[2]\n'
        '    unit = root.name + "-gateway.service"\n'
        '    if action == "drain":\n'
        '        status = show(unit)\n'
        '        actual = proc(int(status["MainPID"]))\n'
        '        deadline = json.loads((root / "run/packet.json").read_text())["window"]["recovery_deadline"]\n'
        '        durable(root / "home" / "control-state.json", {"gateway_state": "draining"})\n'
        '        durable(root / "home" / ".drain_request.json", {"action": "drain", "requested_at": int(time.time())})\n'
        '        durable(root / "run" / "hold.json", {"kind": "release-admission-hold", "repo": str(root / "repo"),\n'
        '                "unit": unit, "pid": actual["pid"], "starttime": actual["starttime"],\n'
        '                "observed": int(time.time()), "approved": True,\n'
        '                "valid_until": int(time.time()) + 3600, "recovery_deadline": deadline,\n'
        '                "owner": "fixture", "recovery_owner": "fixture",\n'
        '                "coverage": {key: "fixture external hold" for key in COVERAGE}})\n'
        '        print(json.dumps(prove_drain(root / "home", root / "run" / "hold.json", str(root / "repo"),\n'
        '                              unit, actual["pid"], actual["starttime"], max_age=30, repeat_delay=0.01, recovery_deadline=deadline)))\n'
        '    elif action in ("offline", "recover-offline"):\n'
        '        return 0\n'
        '    elif action in ("smoke", "recover-smoke"):\n'
        '        print(json.dumps(collect_health(root / "run" / "startup.json", root / "run" / "live.json",\n'
        '                                unit, root / "home", 30)))\n'
        '    else:\n'
        '        raise SystemExit(2)\n'
        '\n'
        'if __name__ == "__main__":\n'
        '    raise SystemExit(main())\n'
    )
    (runtime / 'lifecycle.py').chmod(0o600)
    (runtime / 'runbook.md').write_bytes((SCRIPTS / 'README.md').read_bytes())
    (runtime / 'runbook.md').chmod(0o600)
    return runtime


def module_service_repo(root):
    repo = root / 'repo'
    pkg = repo / 'hermes_cli'
    pkg.mkdir(mode=0o700, parents=True)
    (pkg / '__init__.py').write_text('')
    (pkg / '__init__.py').chmod(0o600)
    (pkg / 'main.py').write_text(
        '#!/usr/bin/env python3\n'
        'from __future__ import annotations\n'
        'import json\n'
        'import os\n'
        'import signal\n'
        'import socket\n'
        'import sqlite3\n'
        'import sys\n'
        'import threading\n'
        'import time\n'
        'from datetime import datetime, timezone\n'
        'from pathlib import Path\n'
        '_started = time.time()\n'
        '\n'
        'def _starttime():\n'
        '    text = Path("/proc/%d/stat" % os.getpid()).read_text()\n'
        '    return text[text.rindex(")") + 2:].split()[19]\n'
        '\n'
        'def _atomic_json(path, payload):\n'
        '    tmp = path.with_name(path.name + ".tmp")\n'
        '    tmp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + chr(10))\n'
        '    os.chmod(tmp, 0o600)\n'
        '    os.replace(tmp, path)\n'
        '\n'
        'def _status(home):\n'
        '    pid = os.getpid()\n'
        '    start = _starttime()\n'
        '    now = datetime.now(timezone.utc).isoformat()\n'
        '    state_file = home / "control-state.json"\n'
        '    try:\n'
        '        state_data = json.loads(state_file.read_text())\n'
        '        gateway_state = state_data.get("gateway_state", "running")\n'
        '        active_agents = int(state_data.get("active_agents", 0))\n'
        '    except Exception:\n'
        '        gateway_state = "running"\n'
        '        active_agents = 0\n'
        '    served = state_data.get("served_profiles", ["default"]) if "state_data" in locals() else ["default"]\n'
        '    platforms = state_data.get("platforms") if "state_data" in locals() else None\n'
        '    if not isinstance(platforms, dict):\n'
        '        platforms = {"stub": {"state": "connected", "updated_at": now,\n'
        '                    "writer_pid": pid, "writer_start_time": int(start),\n'
        '                    "error_code": None, "error_message": None, "needs_attention": False}}\n'
        '    profiles = {name: {"home": str(home if name == "default" else home / "profiles" / name),\n'
        '                       "sessions": True, "platforms": {"stub": True}} for name in served}\n'
        '    observation = {"observed": time.time(), "started": _started, "profiles": profiles,\n'
        '                   "running": True, "draining": gateway_state == "draining",\n'
        '                   "work": {"turns": active_agents, "cron": 0, "api": 0, "background": False}}\n'
        '    return {"protocol": 1, "release_observation": observation, "pid": pid, "answering_pid": pid, "start_time": int(start),\n'
        '            "gateway_state": gateway_state, "active_agents": active_agents,\n'
        '            "served_profiles": served, "session_store": {"status": "ok"},\n'
        '            "platforms": platforms}\n'
        '\n'
        'def _serve_control_socket(home):\n'
        '    path = home / "gateway.sock"\n'
        '    try:\n'
        '        path.unlink()\n'
        '    except FileNotFoundError:\n'
        '        pass\n'
        '    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n'
        '    sock.bind(str(path))\n'
        '    os.chmod(path, 0o600)\n'
        '    sock.listen(5)\n'
        '    while True:\n'
        '        conn, _ = sock.accept()\n'
        '        with conn:\n'
        '            raw = conn.recv(65536)\n'
        '            try:\n'
        '                request = json.loads(raw.splitlines()[0].decode())\n'
        '                verb = request.get("verb")\n'
        '                status = _status(home)\n'
        '                result = {"protocol": 1, "pid": os.getpid(), "start_time": int(_starttime()),\n'
        '                          "profile": "default", "served_profiles": status.get("served_profiles"),\n'
        '                          "hermes_home": str(home)} if verb == "identify" else status\n'
        '                response = {"ok": True, "protocol": 1, "id": request.get("id"), "result": result}\n'
        '            except Exception as exc:\n'
        '                response = {"ok": False, "protocol": 1, "error": type(exc).__name__}\n'
        '            conn.sendall(json.dumps(response).encode() + b"\\n")\n'
        '\n'
        'def _prepare_state(home):\n'
        '    home.mkdir(mode=0o700, parents=True, exist_ok=True)\n'
        '    cron = home / "cron"\n'
        '    cron.mkdir(mode=0o700, exist_ok=True)\n'
        '    now = str(time.time())\n'
        '    (cron / "ticker_heartbeat").write_text(now)\n'
        '    (cron / "ticker_last_success").write_text(now)\n'
        '    conn = sqlite3.connect(home / "state.db")\n'
        '    try:\n'
        '        conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY)")\n'
        '        conn.execute("CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY)")\n'
        '        conn.commit()\n'
        '    finally:\n'
        '        conn.close()\n'
        '    _atomic_json(home / "control-state.json", {"gateway_state": "running"})\n'
        '\n'
        'def main():\n'
        '    if sys.argv[1:] != ["gateway", "run"]:\n'
        '        raise SystemExit(2)\n'
        '    home = Path(os.environ["HERMES_HOME"]).resolve()\n'
        '    _prepare_state(home)\n'
        '    env_record = {"dont_write_bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE"),\n'
        '                  "pycache_prefix": os.environ.get("PYTHONPYCACHEPREFIX")}\n'
        '    _atomic_json(home.parent / "run" / "launcher-env.json", env_record)\n'
        '    threading.Thread(target=_serve_control_socket, args=(home,), daemon=True).start()\n'
        '    while True:\n'
        '        signal.pause()\n'
        '\n'
        'if __name__ == "__main__":\n'
        '    raise SystemExit(main())\n'
    )
    (pkg / 'main.py').chmod(0o600)
    (repo / 'app.txt').write_text('known-good\n')
    return repo


def module_service_start(root, python_exe):
    unit = root.name + '-gateway.service'
    unit_path = Path('/run/user') / str(os.getuid()) / 'systemd/user' / unit
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(
        '[Unit]\nDescription=Disposable module-launcher service\n'
        '[Service]\nType=exec\nRestart=no\nUMask=0077\n'
        'UnsetEnvironment=PYTHONPATH PYTHONHOME PYTHONUSERBASE PYTHONPYCACHEPREFIX\n'
        'Environment=HERMES_HOME=' + str(root / 'home') + ' PYTHONNOUSERSITE=1\n'
        'WorkingDirectory=' + str(root / 'repo') + '\n'
        'ExecStart=' + str(python_exe) + ' ' + str(root / 'runtime' / 'wire_launcher.py') +
        ' --repo ' + str(root / 'repo') + ' --startup-json ' + str(root / 'run' / 'startup.json') +
        ' -- -m hermes_cli.main gateway run\n'
    )
    unit_path.chmod(0o600)
    c.raw(['/usr/bin/systemctl', '--user', 'daemon-reload'], root, 10, c.system_env())
    c.raw(['/usr/bin/systemctl', '--user', 'start', unit], root, 10, c.system_env())
    wait_for(lambda: (root / 'home/cron/ticker_last_success').exists() and _module_socket_status(root / 'home'))
    return unit, unit_path


def module_service_packet(root, operation='stage', python_exe=TEST_PYTHON):
    os.umask(0o077)
    root = Path(root).absolute()
    root.mkdir(mode=0o700)
    for name in ('runtime', 'run', 'repo', 'remote.git', 'git-home', 'home'):
        (root / name).mkdir(mode=0o700)
    c.durable(root / 'sandbox.json', {'task': 't_acd2041b', 'disposable': True}, exclusive=True)
    (root / 'sole-writer.lock').touch(mode=0o600)
    module_service_runtime(root)
    repo = module_service_repo(root)
    def git_repo(*args):
        return c.git(str(repo), *args)

    def git_remote(*args):
        return c.git(str(root / 'remote.git'), *args)

    git_repo('init', '-b', 'main')
    git_remote('init', '--bare', '-b', 'main')
    git_repo('config', 'user.name', 'Disposable test')
    git_repo('config', 'user.email', 'sandbox@example.invalid')
    git_repo('add', 'app.txt', 'hermes_cli/__init__.py', 'hermes_cli/main.py')
    git_repo('commit', '-m', 'known-good fixture')
    old = git_repo('rev-parse', 'HEAD')
    git_repo('branch', 'rollback')
    git_repo('switch', '-c', 'staging')
    (repo / 'app.txt').write_text('candidate\n')
    git_repo('commit', '-am', 'candidate fixture')
    candidate = git_repo('rev-parse', 'HEAD')
    git_repo('push', str(root / 'remote.git'), 'main', 'staging')
    if operation == 'stage':
        git_repo('switch', 'main')
    current_branch = 'main' if operation == 'stage' else 'staging'
    current_sha = old if operation == 'stage' else candidate
    unit, unit_path = module_service_start(root, Path(c.PYTHON))
    now = int(time.time())
    st = repo.stat()
    launch = str(root / 'runtime' / 'wire_launcher.py')
    packet = {
        'version': 2, 'command_timeout': 8, 'installation': str(root / 'runtime'), 'lock': str(root / 'sole-writer.lock'),
        'profile': 'simulated-application', 'repo': str(repo), 'remote': str(root / 'remote.git'),
        'git_route': {'home': str(root / 'git-home'), 'helper': '', 'ssh': ''},
        'git_config': git_repo('config', '--local', '--list').splitlines(),
        'extra_refs': [], 'untracked': [], 'checks': {},
        'drain_path': str(root / 'run/drain-proof.json'), 'health_path': str(root / 'run/runtime-health.json'),
        'repo_identity': {'device': st.st_dev, 'inode': st.st_ino}, 'operation': operation,
        'lock_identity': {'device': (root / 'sole-writer.lock').stat().st_dev, 'inode': (root / 'sole-writer.lock').stat().st_ino},
        'order': 'SIMULATED disposable sandbox: ' + operation + '; verification owner and sole writer: fixture creator. One batch only; rollback source only, no persistent-state restore.',
        'runbook': {'path': str(root / 'runtime' / 'runbook.md'), 'sha256': c.digest((root / 'runtime' / 'runbook.md').read_bytes())},
        'current': c.revision(str(repo), current_sha, current_branch),
        'candidate': c.revision(str(repo), candidate, 'staging'),
        'rollback': c.revision(str(repo), old, 'rollback'),
        'local': {'main': old, 'staging': candidate, 'rollback': old},
        'authoritative': {'main': old, 'staging': candidate},
        'unit': unit, 'unit_definition_sha256': c.digest(c.raw(['/usr/bin/systemctl', '--user', 'cat', unit], '/', env=c.system_env()).encode()),
        'controller_unit': root.name + '-controller.service',
        'launcher': launch, 'launcher_sha256': c.digest(Path(launch).read_bytes()),
        'baseline': c.proc(int(c.show(unit)['MainPID'])),
        'window': {'start': now - 10, 'abort': now + 1200, 'expiry': now + 1570, 'recovery_deadline': now + 1630, 'reserve': 120},
        'receipts': {}, 'commands': [], 'recovery': [],
        'model': {'kind': 'fake', 'name': 'claude-sonnet-5', 'timeout': 1},
        'artifacts': [

            {'path': str(root / 'runtime' / 'controller.py'), 'sha256': c.digest((root / 'runtime' / 'controller.py').read_bytes())},
            {'path': str(root / 'runtime' / 'health.py'), 'sha256': c.digest((root / 'runtime' / 'health.py').read_bytes())},
            {'path': str(root / 'runtime' / 'wire_launcher.py'), 'sha256': c.digest((root / 'runtime' / 'wire_launcher.py').read_bytes())},
            {'path': str(root / 'runtime' / 'runtime_health.py'), 'sha256': c.digest((root / 'runtime' / 'runtime_health.py').read_bytes())},
            {'path': str(root / 'runtime' / 'drain_proof.py'), 'sha256': c.digest((root / 'runtime' / 'drain_proof.py').read_bytes())},
            {'path': str(root / 'runtime' / 'lifecycle.py'), 'sha256': c.digest((root / 'runtime' / 'lifecycle.py').read_bytes())},
            {'path': str(root / 'runtime' / 'runbook.md'), 'sha256': c.digest((root / 'runtime' / 'runbook.md').read_bytes())},

            {'path': str(repo / 'hermes_cli' / '__init__.py'), 'sha256': c.digest((repo / 'hermes_cli' / '__init__.py').read_bytes())},
            {'path': str(repo / 'hermes_cli' / 'main.py'), 'sha256': c.digest((repo / 'hermes_cli' / 'main.py').read_bytes())},
            {'path': str(unit_path), 'sha256': c.digest(unit_path.read_bytes())},
        ],
    }
    for executable in (c.GIT, c.PYTHON, '/usr/bin/systemctl', '/usr/bin/systemd-run'):
        packet['artifacts'].append({'path': executable, 'sha256': c.digest(Path(executable).read_bytes()),
                                    'uid': c.artifact_uid(executable), 'mode': Path(executable).stat().st_mode & 0o777})
    for artifact in packet['artifacts']:
        st = Path(artifact['path']).stat()
        artifact.update(uid=c.artifact_uid(artifact['path']), mode=st.st_mode & 0o777)
    facts = {
        'backup': {'coverage': 'no persistent fixture data', 'restore_verified': 'simulated restore'},
        'compatibility': {k: 'simulated compatible' for k in ('candidate', 'rollback', 'dependencies', 'persistent_state')},
        'drain': {'mechanism': 'fixture has no jobs', 'all_source_consumers': 'fixture service only'},
        'sole_writer': {k: 'fixture creator external hold' for k in ('owner', 'hold', 'writers', 'recovery_handoff')},
        'git_credentials': {'route': c.digest(c.encoded(packet['git_route'])), 'actor': 'fixture creator', 'permission_verified': 'owned bare remote'},
        'authority': {k: 'SIMULATED fixture authority' for k in ('approver', 'provenance', 'batch', 'recovery', 'contact')},
        'readiness': {k: 'SIMULATED no inference' for k in ('probe_path', 'probe_sha256', 'auth_method', 'model', 'cli_sha256')},
        'ci': {'exact_sha': candidate, 'result': 'simulated pass'},
        'review': {'exact_sha': candidate, 'verdict': 'simulated pass'},
    }
    for kind in c.RECEIPTS:
        path = root / 'run' / (kind + '.json')
        c.durable(path, {'order': packet['order'], 'kind': kind, 'candidate': candidate, 'rollback': old,
                         'repo': str(repo), 'unit': unit, 'profile': packet['profile'],
                         'expires': packet['window']['recovery_deadline'], 'approved': True, 'facts': facts[kind],
                         'evidence': 'SIMULATED disposable sandbox: no real data, application or CI approval.'})
        packet['receipts'][kind] = {'path': str(path), 'sha256': c.digest(path.read_bytes())}
    for name in ('drain', 'offline', 'smoke', 'recover-offline', 'recover-smoke'):
        packet['checks'][name] = {'id': name, 'argv': [c.PYTHON, str(root / 'runtime' / 'lifecycle.py'), str(root), name],
                                  'cwd': str(root / 'runtime'), 'env': {**c.BASE_ENV, 'HERMES_HOME': str(root / 'home')}, 'timeout': 8}
    c.durable(root / 'fault.json', {'point': '', 'kind': ''})
    packet['commands'], packet['recovery'] = c.command_plan(packet)
    c.durable(root / 'run' / 'packet.json', packet, exclusive=True)
    authority = c.digest((root / 'run' / 'packet.json').read_bytes())
    c.durable(root / 'run' / 'digest.json', {'sha256': authority}, exclusive=True)
    return packet, authority


def rewrite(root, packet):
    c.durable(root / 'run/packet.json', packet)
    return c.digest((root / 'run/packet.json').read_bytes())


@pytest.mark.parametrize('value', [True, '1', 1.0, None, [], {}])
def test_exact_integer_types(value):
    with pytest.raises(c.Refusal):
        c.shape({'timeout': value}, {'timeout': int})


def test_duplicate_unknown_and_nonfinite_json():
    for text in ('{"x":1,"x":2}', '{"x":NaN}'):
        with pytest.raises(c.Refusal):
            c.loads(text)
    with pytest.raises(c.Refusal):
        c.shape({'tools': 'Bash'}, {})


@pytest.mark.linux_only
@pytest.mark.parametrize('operation', ['stage', 'promote', 'rollback'])
def test_real_git_happy_paths_and_copy(sandbox, operation):
    root, p, authority = sandbox(operation)
    baseline = p['baseline']['pid']
    check = cli(root, authority)
    assert check.returncode == 0, check.stderr
    assert c.show(p['unit'])['MainPID'] == str(baseline)
    assert not (root / 'run/stopped-intent.json').exists()
    applied = cli(root, authority, '--apply')
    assert applied.returncode == 0, applied.stderr
    assert finished(root)['status'] == 'succeeded'
    expected = p['rollback']['sha'] if operation == 'rollback' else p['candidate']['sha']
    assert c.git(p['repo'], 'rev-parse', 'HEAD') == expected
    assert c.show(p['unit'])['MainPID'] != str(baseline)
    remote = c.git(p['repo'], 'ls-remote', p['remote'], 'refs/heads/main').split()[0]
    assert remote == (expected if operation == 'promote' else p['local']['main'])
    journal = [json.loads(x) for x in (root / 'run/journal.jsonl').read_text().splitlines()]
    accepted = [e['op'] for e in journal if e['event'] == 'proposal-accepted']
    executed = [e['op'] for e in journal if e['event'] == 'intent']
    assert accepted == executed == [cmd['id'] for cmd in p['commands']]
    assert cli(root, authority, '--apply').returncode != 0  # single use
    for path in (root / 'run').iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    assert (root / 'runtime').stat().st_mode & 0o777 == 0o700


@pytest.mark.linux_only
@pytest.mark.parametrize('point,kind,expected', [
    ('stop', 'model-loss', 'rolled-back'),
    ('switch', 'supervisor-loss', 'rolled-back'),
    ('switch', 'timeout', 'rolled-back'),
    ('switch', 'escape', 'rolled-back'),
    ('start', 'startup', 'rolled-back'),
    ('smoke', 'smoke', 'rolled-back'),
    ('switch', 'byte-drift', 'recovery-required'),
    ('switch', 'ref-drift', 'recovery-required'),
    ('stop', 'expiry', 'rolled-back'),
])
def test_recovery_faults(sandbox, point, kind, expected):
    root, p, authority = sandbox(fault_point=point, fault_kind=kind)
    if kind == 'expiry':
        now = int(time.time())
        p['window'].update(abort=now + 75, expiry=now + 445, recovery_deadline=now + 505)
        authority = rewrite(root, p)
    applied = cli(root, authority, '--apply')
    assert applied.returncode == 0, applied.stderr
    r = finished(root, 100 if kind == 'expiry' else 35)
    assert r['status'] == expected, r
    journal = [json.loads(x) for x in (root / 'run/journal.jsonl').read_text().splitlines()]
    assert any(e['event'] == 'fenced' for e in journal)
    assert any(e['event'] == 'recovery-begin' and e['model_used'] is False for e in journal)
    if expected == 'rolled-back':
        assert c.git(p['repo'], 'rev-parse', 'HEAD') == p['rollback']['sha']
        assert (Path(p['repo']) / '.git/HEAD').read_text().strip() == p['rollback']['sha']
        assert c.show(p['unit'])['ActiveState'] == 'active'
    else:
        assert c.show(p['unit'])['ActiveState'] == 'inactive'
        assert '--inspect-recovery' in r['operator_command']
    descendant = root / 'run/descendant.json'
    if descendant.exists():
        pid = json.loads(descendant.read_text())['pid']
        # Reparented zombies have no running code; cgroup membership is the fence proof.
        assert pid not in c.cg_pids(p['baseline']['cgroup'])
        assert pid not in c.cg_pids(c.show(p['controller_unit'])['ControlGroup'] or p['baseline']['cgroup'])


@pytest.mark.linux_only
def test_published_main_never_rewound_after_supervisor_kill(sandbox):
    root, p, authority = sandbox('promote', 'push', 'supervisor-loss')
    assert cli(root, authority, '--apply').returncode == 0
    assert finished(root)['status'] == 'rolled-back'
    assert c.git(p['repo'], 'rev-parse', 'main') == p['candidate']['sha']
    assert c.git(p['repo'], 'ls-remote', p['remote'], 'refs/heads/main').split()[0] == p['candidate']['sha']
    assert c.git(p['repo'], 'rev-parse', 'HEAD') == p['rollback']['sha']


@pytest.mark.linux_only
@pytest.mark.parametrize('case', ['unknown', 'bool', 'command', 'dirty', 'index', 'permission', 'receipt', 'ref', 'remote', 'expiry', 'packet-drift', 'lock', 'lock-replaced', 'journal-mode', 'runbook'])
def test_preflight_denials_never_stop(sandbox, case):
    root, p, authority = sandbox()
    lock = None
    if case == 'unknown':
        p['settings'] = {'permissions': {'allow': ['Bash']}}
    elif case == 'bool':
        p['commands'][0]['timeout'] = True
    elif case == 'command':
        p['commands'][0]['argv'] = ['/bin/sh', '-c', 'echo secret-token']
    elif case == 'dirty':
        (Path(p['repo']) / 'app.txt').write_text('secret-token')
    elif case == 'index':
        (Path(p['repo']) / 'app.txt').write_text('secret-token')
        c.git(p['repo'], 'add', 'app.txt')
    elif case == 'permission':
        (root / 'run/packet.json').chmod(0o644)
    elif case == 'receipt':
        (root / 'run/ci.json').write_text('secret-token')
    elif case == 'ref':
        c.git(p['repo'], 'branch', '-f', 'staging', p['rollback']['sha'])
    elif case == 'remote':
        c.git(p['remote'], 'branch', '-f', 'staging', p['rollback']['sha'])
    elif case == 'expiry':
        now = int(time.time())
        p['window'].update(start=now - 100, abort=now - 1, expiry=now + 119, recovery_deadline=now + 179)
    elif case == 'packet-drift':
        p['model']['timeout'] = 2
    elif case == 'lock-replaced':
        (root / 'sole-writer.lock').rename(root / 'old.lock')
        (root / 'sole-writer.lock').touch(mode=0o600)
    elif case == 'journal-mode':
        (root / 'run/journal.jsonl').touch(mode=0o644)
        (root / 'run/journal.jsonl').chmod(0o644)
    elif case == 'runbook':
        (root / 'runtime/runbook.md').write_text('secret-token')
    elif case == 'lock':
        lock = (root / 'sole-writer.lock').open('r+')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if case in ('unknown', 'bool', 'command', 'expiry', 'packet-drift'):
        changed_authority = rewrite(root, p)
        if case != 'packet-drift':
            authority = changed_authority
    try:
        check = cli(root, authority, '--apply')
        assert check.returncode != 0
        assert 'secret-token' not in check.stdout + check.stderr
        assert not (root / 'run/stopped-intent.json').exists()
        assert c.show(p['unit'])['MainPID'] == str(p['baseline']['pid'])
        assert result(root)['status'] == 'preflight-blocked'
        assert cli(root, authority, '--inspect-recovery').returncode == 0
    finally:
        if lock:
            lock.close()


@pytest.mark.linux_only
def test_recovery_refuses_unfenced_takeover(sandbox):
    root, p, authority = sandbox()
    c.durable(root / 'run/stopped-intent.json', {'authority': authority})
    check = cli(root, authority, '--recover')
    assert check.returncode != 0
    assert result(root)['status'] == 'recovery-required'
    assert c.show(p['unit'])['MainPID'] == str(p['baseline']['pid'])


@pytest.mark.linux_only
def test_recovery_deadline_cannot_start(sandbox):
    root, p, authority = sandbox()
    now = int(time.time())
    p['window'] = {'start': now - 400, 'abort': now - 300, 'expiry': now - 180,
                   'recovery_deadline': now - 60, 'reserve': 120}
    authority = rewrite(root, p)
    c.durable(root / 'run/stopped-intent.json', {'authority': authority})
    check = cli(root, authority, '--recover')
    assert check.returncode != 0
    assert result(root)['status'] == 'recovery-required'
    assert c.show(p['unit'])['MainPID'] == str(p['baseline']['pid'])


@pytest.mark.linux_only
def test_submitting_service_exit_and_gateway_shutdown_survival(sandbox):
    root, p, authority = sandbox()
    unit = root.name + '-submitter.service'
    argv = ['/usr/bin/systemd-run', '--user', '--wait', '--collect', '--unit=' + unit,
            '--property=Type=exec', '--property=Restart=no', '--property=UMask=0077',
            '--property=WorkingDirectory=' + str(root / 'run'),
            c.PYTHON, str(root / 'runtime/controller.py'), str(root / 'run/packet.json'),
            '--authority', authority, '--apply']
    c.raw(argv, root, 20, c.system_env())
    assert c.show(unit)['ActiveState'] == 'inactive'
    assert finished(root)['status'] == 'succeeded'
    ready = json.loads((root / 'run/ready.json').read_text())
    assert ready['cgroup'] != p['baseline']['cgroup']
    assert ready['cgroup'].endswith('/' + p['controller_unit'])
    assert not Path('/proc', str(p['baseline']['pid'])).exists()


@pytest.mark.parametrize('proposal', [{'op': 'abort'}, {'op': 'start'}, {'op': 'stop', 'argv': ['/bin/sh']}, 'stop', {'op': True}])
def test_model_cannot_supply_commands_or_skip_order(proposal):
    with pytest.raises(c.Refusal):
        c.accept_proposal(proposal, 'stop')
    c.accept_proposal({'op': 'stop'}, 'stop')


@pytest.mark.linux_only
@pytest.mark.parametrize('case', ['python_escape', 'fault_key', 'wrong_unit', 'bad_receipt_facts', 'unsafe_git_config', 'drain_jobs', 'artifact_hash'])
def test_general_packet_rejects_unsafe_plans(sandbox, case):
    root, p, authority = sandbox()
    if case == 'python_escape':
        p['checks']['offline']['argv'] = [c.PYTHON, '-c', 'print("unsafe")']
    elif case == 'fault_key':
        p['fault'] = {'point': 'stop', 'kind': 'supervisor-loss'}
    elif case == 'wrong_unit':
        p['commands'][1]['argv'][-1] = 'unapproved.service'
    elif case == 'bad_receipt_facts':
        path = root / 'run/compatibility.json'
        receipt = json.loads(path.read_text())
        receipt['facts'].pop('persistent_state')
        c.durable(path, receipt)
        p['receipts']['compatibility']['sha256'] = c.digest(path.read_bytes())
    elif case == 'unsafe_git_config':
        c.git(p['repo'], 'config', 'core.fsmonitor', '!echo unsafe')
        p['git_config'] = c.git(p['repo'], 'config', '--local', '--list').splitlines()
    elif case == 'drain_jobs':
        # The trusted fixture emits a jobs>0 observation. The production executor
        # must refuse BEFORE stop, not merely after a successful raw systemctl stop.
        p['checks']['drain']['argv'][-1] = 'busy'
    elif case == 'artifact_hash':
        p['artifacts'][-1]['sha256'] = '0' * 64
    if case not in ('wrong_unit', 'fault_key'):
        p['commands'], p['recovery'] = c.command_plan(p)
    authority = rewrite(root, p)
    check = cli(root, authority, '--apply')
    if case == 'drain_jobs':
        assert check.returncode == 0, check.stderr
        assert finished(root)['status'] == 'preflight-blocked'
    else:
        assert check.returncode != 0
    assert not (root / 'run/stopped-intent.json').exists()
    assert c.show(p['unit'])['MainPID'] == str(p['baseline']['pid'])


@pytest.mark.linux_only
def test_general_inventory_accepts_classified_files_and_unrelated_worktree(sandbox):
    root, p, authority = sandbox()
    repo = Path(p['repo'])
    path = repo / 'local-state.txt'
    path.write_text('retained fixture state\n')
    p['untracked'] = [{'path': path.name, 'sha256': c.digest(path.read_bytes()),
                       'mode': path.stat().st_mode & 0o777, 'classification': 'backed-up',
                       'reason': 'SIMULATED verified fixture backup receipt'}]
    c.git(repo, 'worktree', 'add', '-b', 'feature/unrelated', str(root / 'unrelated'), 'main')
    p['extra_refs'] = [{'ref': 'refs/heads/feature/unrelated', 'sha': p['local']['main']}]
    c.git(repo, 'remote', 'add', 'origin', p['remote'])
    p['git_config'] = c.git(repo, 'config', '--local', '--list').splitlines()
    authority = rewrite(root, p)
    check = cli(root, authority, '--apply')
    assert check.returncode == 0, check.stderr
    assert finished(root)['status'] == 'succeeded'
    assert path.read_text() == 'retained fixture state\n'
    assert c.git(repo, 'rev-parse', 'feature/unrelated') == p['local']['main']


@pytest.mark.linux_only
def test_health_uses_startup_evidence_not_mutable_head(sandbox):
    root, p, authority = sandbox()
    sys.path.insert(0, str(SCRIPTS))
    from health import read_health
    sys.path.remove(str(SCRIPTS))
    original = json.loads((root / 'health.json').read_text())
    c.durable(root / 'startup.json', {k: original[k] for k in ('pid', 'starttime', 'sha', 'source', 'bytes', 'loaded', 'executable_sha256')})
    live = {**{k: original[k] for k in ('pid', 'starttime', 'platform', 'scheduler', 'persistence', 'sessions')},
            'served_profile_homes': {'default': p['repo']}, 'observed': int(time.time())}
    c.durable(root / 'live.json', live)
    # Deliberate fixture-only source drift while its toy service lives. No controller
    # is authorized to do this; the health hook must still report what was loaded.
    c.git(p['repo'], 'switch', 'staging')
    assert read_health(root / 'startup.json', root / 'live.json', p['unit'])['sha'] == p['current']['sha']
    live['starttime'] = '0'
    c.durable(root / 'live.json', live)
    with pytest.raises(c.Refusal):
        read_health(root / 'startup.json', root / 'live.json', p['unit'])


@pytest.mark.linux_only
def test_explicit_isolated_git_route_with_real_nonforce_push(sandbox):
    root, p, authority = sandbox('promote')
    # A pinned fake SSH transport executes real upload/receive-pack against this
    # task's disposable bare repo. Remote authority and authentication are simulated.
    transport = root / 'runtime/fixture-ssh'
    transport.write_text('#!/usr/bin/python3\nfrom fixture import ssh_transport\nfrom pathlib import Path\n'
                         'ssh_transport(Path(' + repr(str(root)) + '))\n')
    transport.chmod(0o700)
    p['git_route']['ssh'] = str(transport)
    p['remote'] = 'fixture@example.invalid:' + str(root / 'remote.git')
    for executable in (str(transport), '/usr/bin/git-upload-pack', '/usr/bin/git-receive-pack'):
        st = Path(executable).stat()
        p['artifacts'].append({'path': executable, 'sha256': c.digest(Path(executable).read_bytes()),
                               'uid': c.artifact_uid(executable), 'mode': st.st_mode & 0o777})
    path = root / 'run/git_credentials.json'
    receipt = json.loads(path.read_text())
    receipt['facts']['route'] = c.digest(c.encoded(p['git_route']))
    c.durable(path, receipt)
    p['receipts']['git_credentials']['sha256'] = c.digest(path.read_bytes())
    p['commands'], p['recovery'] = c.command_plan(p)
    authority = rewrite(root, p)
    check = cli(root, authority, '--apply')
    assert check.returncode == 0, check.stderr
    assert finished(root)['status'] == 'succeeded'
    assert c.git(str(root / 'remote.git'), 'rev-parse', 'main') == p['candidate']['sha']
    transport_evidence = json.loads((root / 'ssh-environment.json').read_text())
    assert transport_evidence == {'home': p['git_route']['home'], 'inherited_credentials': False}


@pytest.mark.linux_only
def test_general_preflight_does_not_require_fixture_admission(sandbox):
    root, p, authority = sandbox()
    marker = root / 'sandbox.json'
    marker.rename(root / 'saved-sandbox.json')
    try:
        check = cli(root, authority)
        assert check.returncode == 0, check.stderr
        assert not (root / 'run/stopped-intent.json').exists()
    finally:
        (root / 'saved-sandbox.json').rename(marker)


def pin_file(p, path):
    p['artifacts'].append({'path': str(path), 'sha256': c.digest(path.read_bytes()),
                           'uid': c.artifact_uid(path), 'mode': path.stat().st_mode & 0o777})


def replace_artifact(p, path):
    file = Path(path)
    target = str(file)
    for artifact in p['artifacts']:
        if artifact['path'] == target:
            artifact.update({'sha256': c.digest(file.read_bytes()),
                             'uid': c.artifact_uid(file),
                             'mode': file.stat().st_mode & 0o777})
            return
    raise AssertionError(f'missing artifact entry: {target}')


def pin_claude_isolated_launcher_fixture(root, p):
    launch_path = root / 'runtime' / 'fixture.py'
    observer_path = root / 'runtime' / 'runtime_observation.py'
    launch_path.write_text(
        '#!/usr/bin/python3\n'
        'import json, os, signal, sys\n'
        'from pathlib import Path\n'
        'def main():\n'
        '    startup = Path(sys.argv[sys.argv.index("--startup-json") + 1]).resolve()\n'
        '    root = startup.parents[1]\n'
        '    stat = Path(f"/proc/{os.getpid()}/stat").read_text()\n'
        '    starttime = stat[stat.rindex(")") + 2:].split()[19]\n'
        '    (root / "health.json").write_text(json.dumps({"pid": os.getpid(), "starttime": starttime}) + "\\n")\n'
        '    while True:\n'
        '        signal.pause()\n'
        'if __name__ == "__main__":\n'
        '    main()\n',
        encoding='utf-8',
    )
    launch_path.chmod(0o600)
    replace_artifact(p, launch_path)
    for source, dest in ((SCRIPTS / 'runtime_observation.py', observer_path),
                         (SCRIPTS / 'runtime_health.py', root / 'runtime' / 'runtime_health.py'),
                         (SCRIPTS / 'drain_proof.py', root / 'runtime' / 'drain_proof.py')):
        dest.write_bytes(source.read_bytes())
        dest.chmod(0o600)
        pin_file(p, dest)
    dependency_root = root / 'runtime' / 'dependency-root'
    dependency_root.mkdir(mode=0o700)
    dependency_marker = dependency_root / 'fixture-dependency.txt'
    dependency_marker.write_text('pinned fixture dependency\n')
    dependency_marker.chmod(0o600)
    dependency_paths = [str(dependency_root)]
    selected_prefix = Path(c.PYTHON).parent.parent.resolve()
    venv_config = str(selected_prefix / 'pyvenv.cfg') if (selected_prefix / 'pyvenv.cfg').is_file() else ''
    dependencies = root / 'run' / 'launcher-dependencies.json'
    c.durable(dependencies, {'paths': dependency_paths, 'files': launcher.dependency_files(dependency_paths, venv_config),
                             'observer_sha256': c.digest(observer_path.read_bytes()), 'venv_config': venv_config})
    pin_file(p, dependencies)
    for path in c.loads(dependencies.read_bytes())['files']:
        pin_file(p, Path(path))
    p['launcher'] = str(launch_path)
    p['launcher_sha256'] = c.digest(launch_path.read_bytes())
    argv = [c.PYTHON, '-I', '-S', '-B', '-X', 'pycache_prefix=/dev/null',
            str(launch_path), '--repo', p['repo'],
            '--startup-json', str(root / 'run' / 'startup.json'),
            '--dependencies', str(dependencies),
            '--dependencies-sha256', c.digest(dependencies.read_bytes()),
            '--', '-m', 'hermes_cli.main', 'gateway', 'run']
    unit_path = Path(c.show(p['unit'])['FragmentPath'])
    c.raw(['/usr/bin/systemctl', '--user', 'stop', p['unit']], root, 5, c.system_env())
    (root / 'health.json').unlink(missing_ok=True)
    unit_path.write_text('[Unit]\nDescription=Disposable release controller test\n[Service]\n'
                         'Type=exec\nRestart=no\nUMask=0077\nKillMode=control-group\nTimeoutStopSec=3\n'
                         'RuntimeMaxSec=1800\nStandardOutput=null\nStandardError=null\n'
                         'WorkingDirectory=' + str(root / 'runtime') + '\n'
                         'ExecStart=' + c.shlex.join(argv) + '\n',
                         encoding='utf-8')
    unit_path.chmod(0o600)
    c.raw(['/usr/bin/systemctl', '--user', 'daemon-reload'], root, 10, c.system_env())
    c.raw(['/usr/bin/systemctl', '--user', 'start', p['unit']], root, 5, c.system_env())
    f.wait_health(root)
    p['unit_definition_sha256'] = c.digest(c.raw(['/usr/bin/systemctl', '--user', 'cat', p['unit']],
                                                 '/', env=c.system_env()).encode())
    replace_artifact(p, unit_path)
    p['baseline'] = c.proc(int(c.show(p['unit'])['MainPID']))


def test_p1_preserved_readiness(sandbox):
    root, p, authority = sandbox()
    probe_fixture = FIXTURES / 'claude-probe.json'
    probe = c.loads(probe_fixture.read_bytes())
    p['model']['kind'] = 'claude'
    pin_claude_isolated_launcher_fixture(root, p)
    path = root / 'run/readiness.json'
    receipt = c.loads(path.read_bytes())
    probe_path = root / 'run' / 'probe.json'
    c.durable(probe_path, probe)
    cli_path = root / 'runtime' / 'claude-ready'
    cli_path.write_text('#!/usr/bin/python3\nimport sys\nsys.exit(0)\n')
    cli_path.chmod(0o700)
    runtime_controller = root / 'runtime' / 'controller.py'
    runtime_controller.write_text(runtime_controller.read_text().replace('/home/brian/.local/bin/claude', str(cli_path)))
    replace_artifact(p, runtime_controller)
    pin_file(p, cli_path)
    receipt['facts'] = {'probe_path': str(probe_path), 'probe_sha256': c.digest(probe_path.read_bytes()),
                        'auth_method': probe['authMethod'], 'model': p['model']['name'],
                        'cli_sha256': c.digest(cli_path.read_bytes())}
    c.durable(path, receipt)
    p['receipts']['readiness']['sha256'] = c.digest(path.read_bytes())
    check = cli(root, rewrite(root, p))
    assert check.returncode == 0, check.stderr
    assert c.show(p['unit'])['MainPID'] == str(p['baseline']['pid'])
    assert not (root / 'run/stopped-intent.json').exists()


def test_p1_preserved_proposal(monkeypatch, tmp_path):
    answer = c.loads((FIXTURES / 'claude-probe.json').read_bytes())
    assert answer['models'] == list(answer['modelUsage'])
    # The preserved readiness envelope doubles as the accounting fixture.
    answer['result'] = '{"op":"stop"}'
    run = object.__new__(c.Run)
    run.p = {'model': {'kind': 'claude', 'name': 'claude-sonnet-5', 'timeout': 1},
             'runbook': {'path': str(tmp_path / 'runbook')}}
    run.state = tmp_path
    (tmp_path / 'runbook').write_text('frozen')
    (tmp_path / 'runbook').chmod(0o600)
    (tmp_path / 'journal.jsonl').write_text('')
    run.gate = lambda *args: None
    run.locked = lambda: None
    run.model_plan = lambda: {}
    run.log = lambda *args, **kwargs: None
    monkeypatch.setattr(c, 'raw', lambda *args: c.encoded(answer).decode())
    run.propose({'id': 'stop', 'timeout': 1})


def test_p1_nonexecutable_preflight(sandbox):
    root, p, authority = sandbox()
    script = root / 'runtime/nonexecutable-check'
    script.write_text('#!/usr/bin/python3\nprint("offline ok")\n')
    script.chmod(0o600)
    p['checks']['offline']['argv'] = [str(script)]
    pin_file(p, script)
    p['commands'], p['recovery'] = c.command_plan(p)
    authority = rewrite(root, p)
    for flags in ((), ('--apply',)):
        check = cli(root, authority, *flags)
        assert check.returncode != 0
        assert result(root)['status'] == 'preflight-blocked'
        assert c.show(p['unit'])['MainPID'] == str(p['baseline']['pid'])
        assert not (root / 'run/stopped-intent.json').exists()


def test_p1_runtime_fences_stopped_supervisor_and_term_ignoring_setsid(sandbox):
    root, p, authority = sandbox()
    script = root / 'runtime/stalled-check.py'
    script.write_text('import os, signal, time\nfrom pathlib import Path\n'
                      'os.setsid()\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                      'Path(' + repr(str(root / 'run/survivor')) + ').write_text(str(os.getpid()))\n'
                      'os.kill(os.getppid(), signal.SIGSTOP)\ntime.sleep(120)\n')
    script.chmod(0o600)
    pin_file(p, script)
    p['checks']['offline']['argv'] = [c.PYTHON, str(script)]
    p['command_timeout'] = 1
    for cmd in p['checks'].values():
        cmd['timeout'] = 1
    now = int(time.time())
    p['window'].update(abort=now + 30, expiry=now + 175, recovery_deadline=now + 175, reserve=45)
    p['commands'], p['recovery'] = c.command_plan(p)
    authority = rewrite(root, p)
    assert cli(root, authority).returncode == 0
    applied = cli(root, authority, '--apply')
    assert applied.returncode == 0, applied.stderr
    wait_for(lambda: (root / 'run/survivor').exists())
    policy = c.show(p['controller_unit'])
    c.durable(root / 'controller-policy.json', policy)
    assert policy['KillSignal'] == policy['FinalKillSignal'] == '9'
    assert policy['TimeoutStopFailureMode'] == 'kill'
    assert c.show(p['unit'])['KillSignal'] == '15'  # gateway stays graceful
    supervisor = int(policy['MainPID'])
    assert Path('/proc', str(supervisor), 'status').read_text().split('State:')[1].lstrip().startswith('T')
    assert finished(root, 100)['status'] == 'rolled-back'
    rows = [c.loads(line) for line in (root / 'run/journal.jsonl').read_text().splitlines()]
    events = [r['event'] for r in rows]
    assert events.index('fenced') < events.index('recovery-begin')
    assert c.show(p['unit'])['ActiveState'] == 'active'
    assert c.git(p['repo'], 'rev-parse', 'HEAD') == p['rollback']['sha']
    assert int((root / 'run/survivor').read_text()) not in c.cg_pids(c.show(p['controller_unit'])['ControlGroup'] or p['baseline']['cgroup'])
    journal = c.raw(['/usr/bin/journalctl', '--user', '-u', p['controller_unit'], '--no-pager'], '/', env=c.system_env())
    (root / 'systemd-journal.txt').write_text(journal)
    assert 'runtime time limit' in journal.lower()


@pytest.mark.parametrize('case', ['unknown', 'absent', 'error', 'usage-list', 'usage-empty', 'usage-value', 'result-type', 'envelope'])
def test_p1_accounting_denials(case):
    answer = c.loads((FIXTURES / 'claude-probe.json').read_bytes())
    if case == 'unknown':
        answer['modelUsage']['unverified-model'] = {'inputTokens': 1}
    elif case == 'absent':
        del answer['modelUsage']['claude-sonnet-5']
    elif case == 'error':
        answer['is_error'] = True
    elif case == 'usage-list':
        answer['modelUsage'] = list(answer['modelUsage'])
    elif case == 'usage-empty':
        answer['modelUsage'] = {}
    elif case == 'usage-value':
        answer['modelUsage']['claude-sonnet-5'] = None
    elif case == 'result-type':
        answer['result'] = {'op': 'stop'}
    else:
        answer = []
    with pytest.raises(c.Refusal):
        c.inference_accounting(answer, 'claude-sonnet-5')


@pytest.mark.parametrize('models', [[], ['claude-haiku-4-5-20251001'], ['claude-sonnet-5', 'unknown'],
                                    ['claude-sonnet-5', 'claude-sonnet-5'], 'claude-sonnet-5', [True]])
def test_p1_readiness_accounting_denials(models):
    with pytest.raises(c.Refusal):
        c.accounting_models(models, 'claude-sonnet-5')


@pytest.mark.parametrize('kind', ['direct', 'interpreter', 'helper', 'ssh'])
def test_p1_effective_access_and_interpreter_bytes(tmp_path, kind):
    executable = tmp_path / 'python-test'
    executable.write_bytes(Path(c.PYTHON).read_bytes())
    executable.chmod(0o600)
    script = tmp_path / 'check'
    script.write_text('#!' + str(executable) + '\nprint("ok")\n')
    script.chmod(0o700)
    if kind == 'direct':
        argv = [str(executable), str(script)]
    elif kind == 'interpreter':
        argv = [str(script)]
    else:
        # validate_route uses the same command/interpreter path for both routes.
        home = tmp_path / 'git-home'
        home.mkdir(mode=0o700)
        p = {'remote': 'https://example.invalid/repo' if kind == 'helper' else 'git@example.invalid:repo',
             'git_route': {'home': str(home), 'helper': '', 'ssh': ''}}
        p['git_route'][kind] = str(script)
        with pytest.raises(c.Refusal, match='executable access denied'):
            c.validate_route(p)
        return
    with pytest.raises(c.Refusal, match='executable access denied'):
        c.command_files(argv)
    executable.chmod(0o700)
    assert c.command_files([str(script)]) == {str(script), str(executable)}
    script.chmod(0o600)
    assert c.command_files([str(executable), str(script)]) == {str(script), str(executable)}


def test_p1_module_launcher_baseline_helper_preserves_command_deny(tmp_path):
    executable = tmp_path / 'python-test'
    executable.write_bytes(Path(c.PYTHON).read_bytes())
    executable.chmod(0o700)
    argv = [str(executable), '-m', 'hermes_cli.main', 'gateway', 'run']
    with pytest.raises(c.Refusal, match='shell/interpreter escape'):
        c.command_files(argv)
    assert c.baseline_files(argv) == {str(executable)}


def cleanup_module_service(root, unit):
    assert root.name.startswith(f.PREFIX) and unit == root.name + '-gateway.service'
    unit_path = Path('/run/user') / str(os.getuid()) / 'systemd/user' / unit
    for name in (root.name + '-controller.service', unit):
        for action in ('stop', 'reset-failed'):
            subprocess.run(['/usr/bin/systemctl', '--user', action, name], env=c.system_env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    unit_path.unlink(missing_ok=True)
    c.raw(['/usr/bin/systemctl', '--user', 'daemon-reload'], '/', env=c.system_env())
    assert c.show(unit)['MainPID'] == '0'


@pytest.fixture
def module_root(tmp_path):
    try:
        c.raw(['/usr/bin/systemctl', '--user', 'show', '--property=Version'], '/', env=c.system_env())
    except Exception:
        pytest.skip('real user-systemd unavailable; production topology not qualified')
    root = tmp_path.parent / (f.PREFIX + uuid.uuid4().hex[:6])
    try:
        yield root
    finally:
        cleanup_module_service(root, root.name + '-gateway.service')


@pytest.mark.linux_only
def test_module_launcher_baseline_stage_success(module_root):
    root = module_root
    p, authority = module_service_packet(root, 'stage', TEST_PYTHON)
    try:
        check = cli(root, authority)
        assert check.returncode == 0, check.stderr
        assert c.show(p['unit'])['MainPID'] == str(p['baseline']['pid'])
        applied = cli(root, authority, '--apply')
        assert applied.returncode == 0, applied.stderr
        assert finished(root)['status'] == 'succeeded'
        assert c.git(p['repo'], 'rev-parse', 'HEAD') == p['candidate']['sha']
        assert c.show(p['unit'])['MainPID'] != str(p['baseline']['pid'])
        startup = c.loads((root / 'run/startup.json').read_bytes())
        assert startup['sha'] == p['candidate']['sha']
        assert startup['source'] == p['repo']
        assert startup['bytes'] == p['candidate']['files']
        assert any(item['module'] == 'hermes_cli.main' for item in startup['loaded'])
        env = c.loads((root / 'run/launcher-env.json').read_bytes())
        assert env['dont_write_bytecode'] == '1'
        assert Path(env['pycache_prefix']).is_relative_to(root / 'pycache')
    finally:
        cleanup_module_service(root, p['unit'])


@pytest.mark.linux_only
def test_module_launcher_baseline_rollback_success(module_root):
    root = module_root
    p, authority = module_service_packet(root, 'rollback', TEST_PYTHON)
    try:
        assert cli(root, authority).returncode == 0
        applied = cli(root, authority, '--apply')
        assert applied.returncode == 0, applied.stderr
        assert finished(root)['status'] == 'succeeded'
        assert c.git(p['repo'], 'rev-parse', 'HEAD') == p['rollback']['sha']
        assert c.show(p['unit'])['MainPID'] != str(p['baseline']['pid'])
    finally:
        cleanup_module_service(root, p['unit'])


@pytest.mark.linux_only
def test_module_launcher_nonexecutable_baseline_fails_before_stop(module_root):
    root = module_root
    p, authority = module_service_packet(root, 'stage', TEST_PYTHON)
    bad_python = root / 'runtime' / 'python-blocked'
    bad_python.write_bytes(TEST_PYTHON.read_bytes())
    bad_python.chmod(0o600)
    p['baseline']['argv'][0] = str(bad_python)
    p['launcher'] = str(bad_python)
    p['launcher_sha256'] = c.digest(bad_python.read_bytes())
    authority = rewrite(root, p)
    try:
        check = cli(root, authority)
        assert check.returncode != 0
        assert 'executable access denied' in check.stderr
        assert 'shell/interpreter escape' not in check.stderr
        assert not (root / 'run/stopped-intent.json').exists()
        assert c.show(p['unit'])['MainPID'] == str(p['baseline']['pid'])
    finally:
        cleanup_module_service(root, p['unit'])


def test_release_launcher_refuses_dirty_source_before_exec(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir(mode=0o700)
    c.git(repo, 'init', '-b', 'main')
    c.git(repo, 'config', 'user.name', 'Disposable Test')
    c.git(repo, 'config', 'user.email', 'test@example.invalid')
    tracked = repo / 'app.py'
    tracked.write_text('print("clean")\n')
    c.git(repo, 'add', 'app.py')
    c.git(repo, 'commit', '-m', 'clean')
    tracked.write_text('print("dirty")\n')
    run = tmp_path / 'run'
    run.mkdir(mode=0o700)
    env = {key: value for key, value in os.environ.items() if not key.startswith('PYTHON')}
    env['PYTHONNOUSERSITE'] = '1'
    sys.path.insert(0, str(SCRIPTS))
    import launch_gateway as launcher
    sys.path.remove(str(SCRIPTS))
    paths = subprocess.check_output([sys.executable, '-I', '-S', '-B', '-X', 'pycache_prefix=/dev/null', '-c',
                                    'import sys,json,os; print(json.dumps([p for p in sys.path if os.path.isdir(p)]))'], text=True)
    paths = [str(Path(p).resolve()) for p in json.loads(paths)]
    dependencies = run / 'deps.json'
    venv_config = str(Path(sys.prefix) / 'pyvenv.cfg')
    c.durable(dependencies, {'paths': paths, 'files': launcher.dependency_files(paths, venv_config),
                            'observer_sha256': 'unused', 'venv_config': venv_config})
    check = subprocess.run([sys.executable, '-I', '-S', '-B', '-X', 'pycache_prefix=/dev/null', str(SCRIPTS / 'launch_gateway.py'),
                            '--dependencies', str(dependencies), '--dependencies-sha256', c.digest(dependencies.read_bytes()),
                            '--repo', str(repo), '--startup-json', str(run / 'startup.json'),
                            '--', '-m', 'hermes_cli.main', 'gateway', 'run'],
                           env=env, capture_output=True, text=True, timeout=10)
    assert check.returncode == 2
    assert 'tracked working bytes differ from HEAD' in check.stderr
    assert not (run / 'startup.json').exists()


@pytest.mark.linux_only
def test_runtime_health_refuses_stale_scheduler(module_root):
    root = module_root
    p, _authority = module_service_packet(root, 'stage', TEST_PYTHON)
    try:
        (root / 'home/cron/ticker_last_success').write_text(str(time.time() - 120))
        check = subprocess.run([c.PYTHON, str(root / 'runtime/runtime_health.py'),
                                str(root / 'run/startup.json'), str(root / 'run/live.json'),
                                p['unit'], str(root / 'home'), '--max-age', '30', '--startup-timeout', '0'],
                               capture_output=True, text=True, timeout=10, cwd=root / 'runtime')
        assert check.returncode == 2
        assert c.show(p['unit'])['MainPID'] == str(p['baseline']['pid'])
    finally:
        cleanup_module_service(root, p['unit'])


@pytest.mark.linux_only
def test_runtime_health_checks_all_served_profile_homes(module_root):
    root = module_root
    p, _authority = module_service_packet(root, 'stage', TEST_PYTHON)
    try:
        pid = p['baseline']['pid']
        starttime = p['baseline']['starttime']
        worker = root / 'home/profiles/worker'
        (worker / 'cron').mkdir(mode=0o700, parents=True)
        now = str(time.time() - 120)
        (worker / 'cron/ticker_heartbeat').write_text(now)
        (worker / 'cron/ticker_last_success').write_text(now)
        conn = __import__('sqlite3').connect(worker / 'state.db')
        try:
            conn.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY)')
            conn.execute('CREATE TABLE messages (id TEXT PRIMARY KEY)')
            conn.commit()
        finally:
            conn.close()
        fresh = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
        base_platform = {'state': 'connected', 'updated_at': fresh, 'writer_pid': pid,
                         'writer_start_time': int(starttime), 'error_code': None,
                         'error_message': None, 'needs_attention': False}
        c.durable(root / 'home/control-state.json', {
            'gateway_state': 'running',
            'served_profiles': ['default', 'worker'],
            'platforms': {'stub': base_platform, 'worker:stub': base_platform},
        })
        check = subprocess.run([c.PYTHON, str(root / 'runtime/runtime_health.py'),
                                str(root / 'run/startup.json'), str(root / 'run/live.json'),
                                p['unit'], str(root / 'home'), '--max-age', '30', '--startup-timeout', '0'],
                               capture_output=True, text=True, timeout=10, cwd=root / 'runtime')
        assert check.returncode == 2
        assert c.show(p['unit'])['MainPID'] == str(p['baseline']['pid'])
    finally:
        cleanup_module_service(root, p['unit'])


def _module_hold(root, p, value='fixture external hold'):
    coverage = {
        'gateway_turns', 'cron', 'api', 'background_work', 'kanban_workers',
        'updater', 'editors', 'source_readers', 'recovery_handoff',
    }
    hold = {'kind': 'release-admission-hold', 'repo': p['repo'], 'unit': p['unit'],
            'pid': p['baseline']['pid'], 'starttime': p['baseline']['starttime'],
            'observed': int(time.time()), 'approved': True,
            'valid_until': int(time.time()) + 3600, 'recovery_deadline': p['window']['recovery_deadline'],
            'owner': 'fixture', 'recovery_owner': 'fixture',
            'coverage': {key: value for key in coverage}}
    c.durable(root / 'run/hold.json', hold)
    return root / 'run/hold.json'


@pytest.mark.linux_only
def test_drain_proof_refuses_unknown_hold_and_busy_gateway(module_root):
    root = module_root
    p, _authority = module_service_packet(root, 'stage', TEST_PYTHON)
    try:
        c.durable(root / 'home/control-state.json', {'gateway_state': 'draining'})
        hold = _module_hold(root, p, 'unknown')
        argv = [c.PYTHON, str(root / 'runtime/drain_proof.py'), str(root / 'home'),
                str(hold), p['repo'], p['unit'], str(p['baseline']['pid']), p['baseline']['starttime'],
                '--recovery-deadline', str(p['window']['recovery_deadline'])]
        assert subprocess.run(argv, capture_output=True, text=True, timeout=10, cwd=root / 'runtime').returncode == 2
        hold = _module_hold(root, p)
        argv[3] = str(hold)
        c.durable(root / 'home/control-state.json', {'gateway_state': 'draining', 'active_agents': 1})
        assert subprocess.run(argv, capture_output=True, text=True, timeout=10, cwd=root / 'runtime').returncode == 2
        assert c.show(p['unit'])['MainPID'] == str(p['baseline']['pid'])
    finally:
        cleanup_module_service(root, p['unit'])


@pytest.mark.linux_only
def test_gateway_status_and_sqlite_producer_primitives(tmp_path, monkeypatch):
    # Narrow persisted-status primitive assertions; not end-to-end health proof.
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr('tools.tirith_security.ensure_installed', lambda log_failures=False: None)

    from gateway.config import GatewayConfig
    from gateway.control_socket import GatewayControlServer, query_gateway_control
    from gateway.run import GatewayRunner
    from gateway.status import write_runtime_status

    runner = GatewayRunner(GatewayConfig(sessions_dir=home / 'sessions', loop_watchdog=False))
    cron = home / 'cron'
    cron.mkdir(mode=0o700, exist_ok=True)
    now = str(time.time())
    (cron / 'ticker_heartbeat').write_text(now)
    (cron / 'ticker_last_success').write_text(now)
    runner._running = True
    runner._update_runtime_status('running')
    runner._update_platform_runtime_status('local_stub', platform_state='connected', error_code=None, error_message=None)
    write_runtime_status(served_profiles=['default'], session_store={'status': 'ok'})

    async def scenario():
        server = GatewayControlServer(home)
        assert await server.start()
        try:
            loop = __import__('asyncio').get_running_loop()
            identify = await loop.run_in_executor(None, lambda: query_gateway_control(home, 'identify'))
            status = await loop.run_in_executor(None, lambda: query_gateway_control(home, 'status'))
            return identify, status
        finally:
            await server.stop()

    identify, status = __import__('asyncio').run(scenario())
    assert identify['pid'] == os.getpid()
    assert identify['served_profiles'] == ['default']
    assert status['gateway_state'] == 'running'
    assert status['session_store'] == {'status': 'ok'}
    platform = status['platforms']['local_stub']
    assert platform['state'] == 'connected'
    assert platform['writer_pid'] == os.getpid()
    assert (home / 'state.db').exists()
    conn = __import__('sqlite3').connect(f'file:{home / "state.db"}?mode=ro', uri=True)
    try:
        assert conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] >= 0
        assert conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] >= 0
    finally:
        conn.close()


def test_p1_fencing_budget_denied_before_stop(sandbox):
    root, p, authority = sandbox()
    now = int(time.time())
    p['window'].update(abort=now + 120, expiry=now + 240, recovery_deadline=now + 240)
    check = cli(root, rewrite(root, p))
    assert check.returncode != 0
    assert 'reserve not protected' in check.stderr
    assert c.show(p['unit'])['MainPID'] == str(p['baseline']['pid'])
    assert not (root / 'run/stopped-intent.json').exists()


@pytest.mark.parametrize('change', ['survivor', 'KillSignal', 'FinalKillSignal', 'SendSIGKILL', 'TimeoutStopUSec', 'TimeoutStopFailureMode', 'ExecStop'])
def test_p1_guard_refuses_survivor_or_kill_policy_drift(monkeypatch, change):
    # Kernel-uninterruptible survivors cannot be manufactured safely. Exercise
    # the guard's exact observation boundary with an additional cgroup writer.
    import os
    group = '/user.slice/test-controller.service'
    run = object.__new__(c.Run)
    run.p = {'controller_unit': 'test-controller.service', 'baseline': {'cgroup': '/user.slice/gateway.service'},
             'window': {'reserve': 45}}
    run.state = Path('/tmp')
    run.authority = 'frozen-authority'
    run.log = lambda *args, **kwargs: pytest.fail('guard must not record a fence')
    state = {'ControlGroup': group, 'Restart': 'no', 'KillMode': 'control-group',
             'KillSignal': '9', 'FinalKillSignal': '9', 'SendSIGKILL': 'yes',
             'TimeoutStopUSec': '45s', 'TimeoutStopFailureMode': 'kill', 'ExecStop': '',
             'Requires': '', 'Wants': '', 'WorkingDirectory': '/tmp',
             'ExecStopPost': '--recover frozen-authority'}
    if change != 'survivor':
        state[change] = {'KillSignal': '15', 'FinalKillSignal': '15', 'SendSIGKILL': 'no',
                         'TimeoutStopUSec': '46s', 'TimeoutStopFailureMode': 'terminate',
                         'ExecStop': '/unapproved'}[change]
    monkeypatch.setattr(c, 'show', lambda unit: state)
    monkeypatch.setattr(c, 'proc', lambda pid: {'cgroup': group})
    monkeypatch.setattr(c, 'cg_pids', lambda group: {os.getpid(), os.getpid() + 1})
    with pytest.raises(c.Refusal, match='takeover fencing not proven' if change == 'survivor' else 'unsafe supervisor lifecycle'):
        run.topology(recovery=True)


def test_p1_real_guard_survivor_refuses_recovery(sandbox):
    import shlex
    root, p, authority = sandbox()
    wrapper = root / 'runtime/survivor-guard.py'
    # A disposable ExecStopPost helper creates a writer at the guard boundary.
    # This safely exercises real /proc+cgroup refusal without an unkillable task.
    wrapper.write_text('import os, time\nimport controller as c\n'
                       'if os.fork() == 0:\n    time.sleep(120)\n    os._exit(0)\n'
                       'raise SystemExit(c.main())\n')
    wrapper.chmod(0o600)
    pin_file(p, wrapper)
    authority = rewrite(root, p)
    c.durable(root / 'run/stopped-intent.json', {'authority': authority})
    post = shlex.join([c.PYTHON, str(wrapper), str(root / 'run/packet.json'), '--authority', authority, '--recover'])
    props = {'Type': 'exec', 'Restart': 'no', 'UMask': '0077', 'KillMode': 'control-group',
             'KillSignal': 'SIGKILL', 'FinalKillSignal': 'SIGKILL', 'SendSIGKILL': 'yes',
             'TimeoutStopFailureMode': 'kill', 'TimeoutStopSec': str(p['window']['reserve']),
             'WorkingDirectory': str(root / 'run'), 'ExecStopPost': post, 'ProtectControlGroups': 'yes'}
    c.raw(['/usr/bin/systemd-run', '--user', '--unit=' + p['controller_unit'],
           *['--property=' + k + '=' + v for k, v in props.items()], '/usr/bin/true'], '/', env=c.system_env())
    r = finished(root)
    assert r['status'] == 'recovery-required'
    assert r['reason'] == 'takeover fencing not proven'
    rows = [c.loads(line) for line in (root / 'run/journal.jsonl').read_text().splitlines()]
    assert not any(r['event'] in ('intent', 'recovery-begin', 'fenced') for r in rows)
    assert c.show(p['unit'])['MainPID'] == str(p['baseline']['pid'])


@pytest.fixture
def frozen_git_run(tmp_path, monkeypatch):
    """Real Git/ref/index/bytes and controller sequencing; no service/model calls.

    Lifecycle, authority and clock boundaries are simulated here. Their executable
    qualification remains the previously retained disposable-systemd evidence.
    """
    import contextlib

    repo = tmp_path / 'repo'
    repo.mkdir(mode=0o700)
    c.git(repo, 'init', '-b', 'main')
    (repo / '.git').chmod(0o700)
    c.git(repo, 'config', 'user.name', 'Disposable Test')
    c.git(repo, 'config', 'user.email', 'test@example.invalid')
    commits = []
    for content in ('old', 'remote-main', 'candidate'):
        (repo / 'source.py').write_text(content + '\n')
        c.git(repo, 'add', 'source.py')
        c.git(repo, 'commit', '-m', content)
        commits.append(c.git(repo, 'rev-parse', 'HEAD'))
    old, remote_main, candidate = commits
    c.git(repo, 'branch', 'staging', old)
    c.git(repo, 'branch', 'rollback', old)
    c.git(repo, 'switch', 'staging')
    c.git(repo, 'branch', '-f', 'main', old)  # main is NOT checked out.
    remote = tmp_path / 'remote.git'
    c.git(tmp_path, 'init', '--bare', str(remote))
    c.git(repo, 'push', str(remote), remote_main + ':refs/heads/main', candidate + ':refs/heads/staging')
    home = tmp_path / 'git-home'
    home.mkdir(mode=0o700)
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    st = repo.stat()
    baseline = {'pid': 123, 'starttime': '456', 'cgroup': '/simulated', 'argv': ['/simulated']}
    p = {
        'repo': str(repo), 'repo_identity': {'device': st.st_dev, 'inode': st.st_ino},
        'current': c.revision(repo, old, 'staging'),
        'candidate': c.revision(repo, candidate, 'staging'),
        'rollback': c.revision(repo, old, 'rollback'),
        'local': {'main': old, 'staging': old, 'rollback': old},
        'authoritative': {'main': remote_main, 'staging': candidate},
        'extra_refs': [], 'untracked': [],
        'git_config': c.git(repo, 'config', '--local', '--list').splitlines(),
        'git_route': {'home': str(home), 'helper': '', 'ssh': ''}, 'remote': str(remote),
        'operation': 'stage', 'command_timeout': 10, 'unit': 'simulated.service',
        'installation': str(state), 'baseline': baseline, 'launcher': '/simulated',
        'model': {'kind': 'fake', 'timeout': 1},
        'checks': {name: {'id': name, 'timeout': 1} for name in
                   ('drain', 'offline', 'smoke', 'recover-offline', 'recover-smoke')},
    }
    p['commands'], p['recovery'] = c.command_plan(p)
    run = object.__new__(c.Run)
    run.p, run.state, run.authority = p, state, 'simulated-authority'
    run.events, run.started, run.stopped, run.drain_seen = [], [], False, False
    run.fault = lambda phase, op: None
    monkeypatch.setattr(run, 'lock', contextlib.nullcontext)
    for name in ('gate', 'clock_gate', 'topology', 'log', 'propose'):
        monkeypatch.setattr(run, name, lambda *a, **kw: None)
    monkeypatch.setattr(c, 'show', lambda unit: {'ActiveState': 'active', 'MainPID': '123'})
    monkeypatch.setattr(c, 'proc', lambda pid: baseline)
    monkeypatch.setattr(run, 'result', lambda status, reason: setattr(run, 'outcome', status))
    def drained():
        assert run.drain_seen
    def inactive():
        assert run.stopped and run.drain_seen
    monkeypatch.setattr(run, 'drained', drained)
    monkeypatch.setattr(run, 'inactive', inactive)
    monkeypatch.setattr(run, 'verify_runtime', lambda rev: run.bytes_at(rev, '' if run.recovering or p['operation'] == 'rollback' else
                        ('main' if p['operation'] == 'promote' else 'staging')))
    run.recovering = False
    def execute(cmd):
        op = cmd['id']
        run.fault('before', op)
        if op == 'drain':
            run.drain_seen = True
        elif op in ('stop', 'recover-stop'):
            c.durable(state / 'stopped-intent.json', {'authority': run.authority})
            run.stopped = True
        elif op in ('start', 'recover-start'):
            run.started.append(c.git(repo, 'rev-parse', 'HEAD'))
            run.stopped = False
        elif op in ('switch', 'merge', 'push', 'recover-switch'):
            inactive()
            c.raw(cmd['argv'], cmd['cwd'], cmd['timeout'], cmd['env'])
        run.events.append(op)
        run.fault('after', op)
    monkeypatch.setattr(run, 'execute', execute)
    return run, commits


def _detached_commit(repo, message, content):
    path = Path(repo) / 'source.py'
    path.write_text(content + '\n')
    c.git(repo, 'add', 'source.py')
    c.git(repo, 'commit', '-m', message)
    return c.git(repo, 'rev-parse', 'HEAD')


def _merge_candidate(repo, left, right):
    env = {**c.BASE_ENV, 'GIT_AUTHOR_NAME': 'Disposable Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
           'GIT_COMMITTER_NAME': 'Disposable Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}
    tree = c.git(repo, 'rev-parse', left + '^{tree}')
    return c.raw([c.GIT, 'commit-tree', tree, '-p', left, '-p', right, '-m', 'candidate'], repo, env=env)


@pytest.mark.parametrize('operation', ['stage', 'promote'])
def test_frozen_git_main_alignment_accepts_local_main_ahead(frozen_git_run, operation):
    run, (old, remote_main, candidate) = frozen_git_run
    repo = run.p['repo']
    c.git(repo, 'branch', '-f', 'main', remote_main)
    c.git(run.p['remote'], 'update-ref', 'refs/heads/main', old)
    run.p['local']['main'] = remote_main
    run.p['authoritative']['main'] = old
    if operation == 'promote':
        c.git(repo, 'merge', '--ff-only', candidate)
        run.p['local']['staging'] = candidate
        run.p['current'] = run.p['candidate']
    run.p['operation'] = operation
    run.p['commands'], run.p['recovery'] = c.command_plan(run.p)
    run.preflight()


@pytest.mark.parametrize('operation', ['stage', 'promote'])
def test_frozen_git_main_alignment_rejects_diverged_endpoints(frozen_git_run, operation):
    run, (old, remote_main, candidate) = frozen_git_run
    repo = run.p['repo']
    c.git(repo, 'switch', '--detach', old)
    local_main = _detached_commit(repo, 'local-main', 'local-main')
    c.git(repo, 'switch', 'staging')
    c.git(repo, 'switch', '--detach', old)
    authoritative_main = _detached_commit(repo, 'authoritative-main', 'authoritative-main')
    c.git(repo, 'switch', 'staging')
    merged_candidate = _merge_candidate(repo, local_main, authoritative_main)
    c.git(repo, 'branch', '-f', 'main', local_main)
    c.git(repo, 'push', '--force', run.p['remote'], authoritative_main + ':refs/heads/main',
          merged_candidate + ':refs/heads/staging')
    run.p['local']['main'] = local_main
    run.p['authoritative']['main'] = authoritative_main
    run.p['authoritative']['staging'] = merged_candidate
    run.p['candidate'] = c.revision(repo, merged_candidate, 'staging')
    if operation == 'promote':
        c.git(repo, 'switch', '--detach', old)
        c.git(repo, 'branch', '-f', 'staging', merged_candidate)
        c.git(repo, 'switch', 'staging')
        run.p['local']['staging'] = merged_candidate
        run.p['current'] = c.revision(repo, merged_candidate, 'staging')
    else:
        run.p['current'] = c.revision(repo, old, 'staging')
    run.p['operation'] = operation
    run.p['commands'], run.p['recovery'] = c.command_plan(run.p)
    with pytest.raises(c.Refusal, match='main chain divergence'):
        run.preflight()


def test_frozen_git_stage_old_checked_out_branch(frozen_git_run):
    run, (old, remote_main, candidate) = frozen_git_run
    run.preflight()
    assert c.git(run.p['repo'], 'rev-parse', 'HEAD') == old
    run.forward()
    assert run.events == ['drain', 'stop', 'switch', 'merge', 'offline', 'start', 'smoke']
    assert run.started == [candidate] and run.outcome == 'succeeded'
    run.bytes_at(run.p['candidate'], 'staging')
    run.refs(staged=True)
    assert c.git(run.p['repo'], 'rev-parse', 'main') == old
    assert run.remote()['refs/heads/main'] == remote_main


def test_frozen_git_promote_lagging_main(frozen_git_run):
    run, (old, remote_main, candidate) = frozen_git_run
    c.git(run.p['repo'], 'merge', '--ff-only', candidate)
    run.p['local']['staging'] = candidate
    run.p['current'] = run.p['candidate']
    run.p['operation'] = 'promote'
    run.p['commands'], run.p['recovery'] = c.command_plan(run.p)
    run.forward()
    assert run.started == [candidate] and run.outcome == 'succeeded'
    run.bytes_at(run.p['candidate'], 'main')
    run.refs(promoted=True)
    run.recovery_refs()  # Legal completed local and remote promotion.


@pytest.mark.parametrize('operation', ['stage', 'rollback'])
def test_frozen_git_already_aligned_paths(frozen_git_run, operation):
    run, (old, remote_main, candidate) = frozen_git_run
    p = run.p
    c.git(p['repo'], 'merge', '--ff-only', candidate)
    c.git(p['repo'], 'branch', '-f', 'main', remote_main)
    p['local'].update(main=remote_main, staging=candidate)
    p['current'] = p['candidate']
    p['operation'] = operation
    p['commands'], p['recovery'] = c.command_plan(p)
    run.forward()
    assert run.outcome == 'succeeded'
    assert run.started == [old if operation == 'rollback' else candidate]
    run.recovery_refs()


@pytest.mark.parametrize('phase,op', [('before', 'merge'), ('after', 'merge'), ('after', 'push')])
def test_frozen_git_promote_recovery_preserves_publication(frozen_git_run, phase, op):
    run, (old, remote_main, candidate) = frozen_git_run
    p = run.p
    c.git(p['repo'], 'merge', '--ff-only', candidate)
    p['local']['staging'] = candidate
    p['current'] = p['candidate']
    p['operation'] = 'promote'
    p['commands'], p['recovery'] = c.command_plan(p)
    def fail(at, current):
        if (at, current) == (phase, op):
            raise c.Refusal('simulated interruption')
    run.fault = fail
    with pytest.raises(c.Refusal, match='simulated interruption'):
        run.forward()
    run.fault = lambda *args: None
    published = c.git(p['remote'], 'rev-parse', 'main')
    run.recover()
    assert run.outcome == 'rolled-back' and run.started[-1] == old
    assert c.git(p['remote'], 'rev-parse', 'main') == published
    assert published == (candidate if op == 'push' else remote_main)
    run.bytes_at(p['rollback'], '')


@pytest.mark.parametrize('phase,op', [('before', 'switch'), ('after', 'switch'),
                                     ('before', 'merge'), ('after', 'merge'),
                                     ('after', 'offline'), ('after', 'start')])
def test_frozen_git_stage_recovery_endpoints(frozen_git_run, phase, op):
    run, (old, _, candidate) = frozen_git_run
    def fail(at, current):
        if (at, current) == (phase, op):
            raise c.Refusal('simulated interruption')
    run.fault = fail
    with pytest.raises(c.Refusal, match='simulated interruption'):
        run.forward()
    run.fault = lambda *args: None
    expected_staging = c.git(run.p['repo'], 'rev-parse', 'staging')
    run.recover()
    assert run.outcome == 'rolled-back' and run.started[-1] == old
    run.bytes_at(run.p['rollback'], '')
    assert c.git(run.p['repo'], 'rev-parse', 'staging') == expected_staging
    assert expected_staging in (old, candidate)


@pytest.mark.parametrize('case', ['local-staging', 'remote-staging', 'local-main', 'remote-main',
                                  'index', 'bytes', 'branch', 'rollback', 'extra-ref'])
def test_frozen_git_stage_drift_never_starts(frozen_git_run, case):
    run, (old, remote_main, candidate) = frozen_git_run
    repo = run.p['repo']
    def drift(phase, op):
        if (phase, op) != ('after', 'offline'):
            return
        if case == 'local-staging':
            c.git(repo, 'switch', '--detach', candidate)
            c.git(repo, 'branch', '-f', 'staging', old)
            c.git(repo, 'switch', 'staging')
        elif case == 'remote-staging':
            c.git(run.p['remote'], 'update-ref', 'refs/heads/staging', old)
        elif case == 'local-main':
            c.git(repo, 'branch', '-f', 'main', remote_main)
        elif case == 'remote-main':
            c.git(run.p['remote'], 'update-ref', 'refs/heads/main', candidate)
        elif case in ('index', 'bytes'):
            (Path(repo) / 'source.py').write_text('drift\n')
            if case == 'index':
                c.git(repo, 'add', 'source.py')
        elif case == 'branch':
            c.git(repo, 'switch', '--detach', candidate)
        elif case == 'rollback':
            c.git(repo, 'branch', '-f', 'rollback', candidate)
        else:
            c.git(repo, 'branch', 'unexpected', candidate)
    run.fault = drift
    with pytest.raises(c.Refusal):
        run.forward()
    assert run.started == []


@pytest.mark.parametrize('case', ['staging-nonancestor', 'remote-main-nonancestor',
                                  'missing-remote-object', 'rollback-lag', 'promote-old-staging'])
def test_frozen_git_preflight_rejects_unproven_chain(frozen_git_run, case):
    run, (old, remote_main, candidate) = frozen_git_run
    p, repo = run.p, run.p['repo']
    if case == 'staging-nonancestor':
        (Path(repo) / 'source.py').write_text('divergent\n')
        c.git(repo, 'add', 'source.py')
        c.git(repo, 'commit', '-m', 'divergent')
        sha = c.git(repo, 'rev-parse', 'HEAD')
        p['local']['staging'] = sha
        p['current'] = c.revision(repo, sha, 'staging')
    elif case == 'remote-main-nonancestor':
        (Path(repo) / 'source.py').write_text('divergent\n')
        c.git(repo, 'add', 'source.py')
        c.git(repo, 'commit', '-m', 'divergent')
        sha = c.git(repo, 'rev-parse', 'HEAD')
        c.git(repo, 'push', '--force', p['remote'], sha + ':refs/heads/main')
        c.git(repo, 'switch', '--detach', old)
        c.git(repo, 'branch', '-f', 'staging', old)
        c.git(repo, 'switch', 'staging')
        p['authoritative']['main'] = sha
    elif case == 'missing-remote-object':
        # Create a commit only in the disposable bare remote.
        env = {**c.BASE_ENV, 'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
               'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}
        sha = c.raw([c.GIT, 'commit-tree', p['candidate']['tree'], '-p', remote_main, '-m', 'remote-only'],
                    p['remote'], env=env)
        c.git(p['remote'], 'update-ref', 'refs/heads/main', sha)
        p['authoritative']['main'] = sha
    else:
        p['operation'] = 'rollback' if case == 'rollback-lag' else 'promote'
        if case == 'rollback-lag':
            c.git(repo, 'merge', '--ff-only', candidate)
            p['local']['staging'] = candidate
            p['current'] = p['candidate']
    p['commands'], p['recovery'] = c.command_plan(p)
    with pytest.raises(c.Refusal):
        run.forward()
    assert run.events == [] and run.started == []


@pytest.mark.parametrize('case', ['main', 'intermediate-staging', 'remote-main', 'remote-staging', 'rollback', 'extra'])
def test_frozen_git_recovery_rejects_illegal_ref_changes(frozen_git_run, case):
    run, (old, intermediate, candidate) = frozen_git_run
    repo = run.p['repo']
    if case in ('main', 'rollback', 'extra'):
        c.git(repo, 'branch', '-f', case, candidate)
    elif case == 'intermediate-staging':
        c.git(repo, 'merge', '--ff-only', intermediate)
    else:
        branch = case.removeprefix('remote-')
        c.git(run.p['remote'], 'update-ref', 'refs/heads/' + branch, candidate if branch == 'main' else old)
    with pytest.raises(c.Refusal):
        run.recovery_refs()


@pytest.mark.linux_only
@pytest.mark.parametrize('case', ['legacy-baseline', 'missing-dependencies'])
def test_release_launcher_contract_gate_before_stop(module_root, case):
    root = module_root
    p, _ = module_service_packet(root, 'stage', TEST_PYTHON)
    original_pid = p['baseline']['pid']
    try:
        if case == 'legacy-baseline':
            p['baseline']['argv'] = [str(TEST_PYTHON), '-m', 'hermes_cli.main', 'gateway', 'run']
            expected = 'separately approved bootstrap installation and restart'
        else:
            path = root / 'runtime/launch_gateway.py'
            path.write_bytes((SCRIPTS / 'launch_gateway.py').read_bytes())
            path.chmod(0o600)
            p['launcher'] = str(path)
            p['launcher_sha256'] = c.digest(path.read_bytes())
            p['baseline']['argv'] = [str(TEST_PYTHON), '-I', '-S', '-B', '-X', 'pycache_prefix=/dev/null', str(path)]
            pin_file(p, path)
            expected = 'dependency pin required'
        check = cli(root, rewrite(root, p))
        assert check.returncode != 0 and expected in check.stderr
        assert c.show(p['unit'])['MainPID'] == str(original_pid)
        assert not (root / 'run/stopped-intent.json').exists()
    finally:
        cleanup_module_service(root, p['unit'])
