"""Bounded real launcher/runner/ticker/SQLite/control observations, no live unit."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.linux_only

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts/claude_release_switch'
sys.path.insert(0, str(SCRIPTS))
import controller as c
import launch_gateway as launch
import runtime_health as health
import runtime_observation as observation
import health as health_contract
import drain_proof as drain
sys.path.remove(str(SCRIPTS))


def wait(check, timeout=90):
    end = time.monotonic() + timeout
    error = None
    while time.monotonic() < end:
        try:
            value = check()
            if value:
                return value
        except Exception as exc:
            error = exc
        time.sleep(.1)
    raise AssertionError(f'bounded readiness failed: {error}')


@pytest.fixture(scope='module')
def dependency_manifest(tmp_path_factory):
    root = tmp_path_factory.mktemp('deps')
    paths = list(dict.fromkeys(str(Path(p).resolve()) for p in sys.path
                               if p and ('site-packages' in p or '/lib/python3.' in p) and Path(p).is_dir()))
    venv_path = Path(sys.prefix) / 'pyvenv.cfg'
    venv_config = str(venv_path) if venv_path.is_file() else ''
    value = {'paths': paths, 'venv_config': venv_config, 'files': launch.dependency_files(paths, venv_config),
             'observer_sha256': c.digest((SCRIPTS / 'runtime_observation.py').read_bytes())}
    path = root / 'dependencies.json'
    c.durable(path, value)
    return path


def _actual_gateway_root(tmp_path_factory, rollback=False):
    return tmp_path_factory.mktemp('r' if rollback else 'c')


def test_actual_gateway_root_helper_allocates_unique_short_roots(tmp_path_factory):
    roots = [_actual_gateway_root(tmp_path_factory) for _ in range(3)]
    roots.append(_actual_gateway_root(tmp_path_factory, rollback=True))

    assert len(set(roots)) == len(roots)
    assert len({root.parent for root in roots}) == 1
    assert all(root.is_dir() for root in roots)
    assert all(root.name[0] in {'c', 'r'} and len(root.name) <= 4 for root in roots)
    assert all(len(str(root / 'profiles/secondary/gateway.sock')) < 104 for root in roots)


@pytest.fixture
def actual_gateway(tmp_path_factory, dependency_manifest, request):
    if getattr(request, 'param', None):
        available = subprocess.run(['/usr/bin/git', '-C', str(SCRIPTS.parents[1]), 'cat-file', '-e',
                                    request.param + '^{commit}'], capture_output=True, timeout=5)
        if available.returncode:
            pytest.skip('Local rollback qualification requires historical object ' + request.param
                        + '; shallow hosted CI still runs candidate/native/readiness coverage')
    root = _actual_gateway_root(tmp_path_factory, rollback=bool(getattr(request, 'param', None)))
    repo = root / 'repo'
    subprocess.run(['/usr/bin/git', 'clone', '--quiet', '--shared', str(SCRIPTS.parents[1]), str(repo)],
                   check=True, timeout=30, capture_output=True)
    if getattr(request, 'param', None):
        c.git(repo, 'checkout', '--detach', request.param)
    c.git(repo, 'config', 'user.name', 'Disposable Test')
    c.git(repo, 'config', 'user.email', 'test@example.invalid')
    support = Path(__file__).parent / 'fixtures/claude_release_switch/real_gateway_support.py'
    (repo / 'release_test_support.py').write_bytes(support.read_bytes())
    with (repo / 'hermes_cli/main.py').open('a') as out:
        out.write('\nimport release_test_support\nrelease_test_support.install()\n')
    c.git(repo, 'add', 'release_test_support.py', 'hermes_cli/main.py')
    c.git(repo, 'commit', '-qm', 'Disposable credential-free transport fixture')
    home = root
    secondary = home / 'profiles/secondary'
    secondary.mkdir(parents=True)
    # Existing secondary persistence, with no session and no runner handle.
    from hermes_state import SessionDB
    db = SessionDB(db_path=secondary / 'state.db')
    db.close()
    # Profile resolver + ticker see genuine disposable profile directories.
    for path in (home, secondary):
        (path / 'config.yaml').write_text('security:\n  tirith_enabled: false\n')
    # Reviewed dependency inside the checkout, like an operational .venv.
    # This is one module directory, not a new or modified Python environment.
    dep_root = repo / '.fixture-dependency'
    dep_root.mkdir()
    (dep_root / 'release_dependency.py').write_text('value = "reviewed"\n')
    dependency_data = c.loads(dependency_manifest.read_bytes())
    dependency_data['paths'].append(str(dep_root))
    dependency_data['files'].update(launch.dependency_files([str(dep_root)]))
    dependency_manifest = root / 'dependencies.json'
    c.durable(dependency_manifest, dependency_data)
    startup = root / 'startup.json'
    argv = [sys.executable, '-I', '-S', '-B', '-X', 'pycache_prefix=/dev/null', str(SCRIPTS / 'launch_gateway.py'),
            '--repo', str(repo), '--startup-json', str(startup),
            '--dependencies', str(dependency_manifest), '--dependencies-sha256', c.digest(dependency_manifest.read_bytes()),
            '--', '-m', 'hermes_cli.main', 'gateway', 'run']
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(root), 'HERMES_HOME': str(home), 'LANG': 'C.UTF-8'}
    with (root / 'gateway.log').open('w') as log:
        process = subprocess.Popen(argv, env=env, stdout=log, stderr=log, cwd=root)
        process.rollback_fixture = bool(getattr(request, 'param', None))
        try:
            yield root, home, secondary, startup, process
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_final_launch_real_runner_collect_drain(actual_gateway, monkeypatch):
    root, home, secondary, startup, process = actual_gateway
    # Only systemd ownership lookup is substituted; PID/starttime and all
    # application health are obtained from the actual disposable child.
    monkeypatch.setattr(health_contract, 'show', lambda unit: {'ActiveState': 'active', 'MainPID': str(process.pid)})
    def collect():
        if process.poll() is not None:
            pytest.fail((root / 'gateway.log').read_text()[-6000:])
        return health.collect(startup, root / 'live.json', 'disposable.service', home, 90)
    result = wait(collect)
    assert result['healthy']
    assert {'gateway.run', 'hermes_cli.main', 'cron.scheduler'} <= {row['module'] for row in result['loaded']}
    live = c.loads((root / 'live.json').read_bytes())
    assert live['served_profile_homes'] == {'default': str(home), 'secondary': str(secondary)}
    for path in (home, secondary):
        assert (path / 'cron/ticker_heartbeat').exists()
        assert (path / 'cron/ticker_last_success').exists()
        health._check_state_db(path)
    # Observe working transport after the old 30-second freshness boundary.
    if not getattr(process, 'rollback_fixture', False):
        time.sleep(31)
        assert collect()['healthy']
    # No timestamp mutation: the observer simply does not use transition age.
    assert 'release_observation' in health._query_socket(home, 'status')
    (secondary / 'api-busy').touch()
    wait(lambda: health._query_socket(home, 'status')['release_observation']['work']['api'] == 1)
    with pytest.raises(c.Refusal, match='api'):
        collect()
    (secondary / 'api-busy').unlink()
    wait(collect)
    marker = secondary / 'cron-busy'
    marker.touch()
    wait(lambda: health._query_socket(home, 'status')['release_observation']['work']['cron'] == 1)
    with pytest.raises(c.Refusal, match='cron'):
        collect()
    marker.unlink()
    wait(collect)
    (secondary / 'background-busy').touch()
    wait(lambda: health._query_socket(home, 'status')['release_observation']['work']['background'])
    with pytest.raises(c.Refusal, match='background'):
        collect()
    # Existing real external-drain marker/watcher, no fabricated status.
    from gateway.drain_control import write_drain_request
    monkeypatch.setenv('HERMES_HOME', str(home))
    write_drain_request(principal='disposable-test')
    wait(lambda: health._query_socket(home, 'status')['release_observation']['draining'])
    actual = c.proc(process.pid)
    hold = root / 'hold.json'
    recovery_deadline = int(time.time()) + 240
    c.durable(hold, {'kind': 'release-admission-hold', 'repo': str(root / 'repo'), 'unit': 'disposable.service',
                    'pid': process.pid, 'starttime': actual['starttime'], 'observed': int(time.time()),
                    'valid_until': int(time.time()) + 300, 'recovery_deadline': recovery_deadline,
                    'owner': 'disposable test', 'recovery_owner': 'disposable test', 'approved': True,
                    'coverage': {key: 'isolated fixture owns these consumers' for key in drain.COVERAGE}})
    def proof():
        return drain.prove(home, hold, str(root / 'repo'), 'disposable.service', process.pid,
                           actual['starttime'], max_age=90, repeat_delay=.01, recovery_deadline=recovery_deadline)
    with pytest.raises(c.Refusal, match='background'):
        proof()
    (secondary / 'background-busy').unlink()
    assert wait(proof)['active_jobs'] == 0
    from gateway.drain_control import clear_drain_request
    clear_drain_request()
    wait(collect)
    # A recent marker from the previous gateway generation is also refused.
    success_marker = secondary / 'cron/ticker_last_success'
    genuine_marker = success_marker.read_text()
    started = health._query_socket(home, 'status')['release_observation']['started']
    success_marker.write_text(str(started - 1))
    with pytest.raises(c.Refusal, match='predates'):
        collect()
    success_marker.write_text(genuine_marker)  # restore actual producer bytes
    # Genuine ticker created this file; stale secondary must block health.
    (secondary / 'cron/ticker_last_success').write_text(str(time.time() - 1000))
    with pytest.raises(c.Refusal, match='stale'):
        collect()
    (secondary / 'disconnect').touch()
    wait(lambda: not health._query_socket(home, 'status')['release_observation']['profiles']['secondary']['platforms']['discord'])
    with pytest.raises(c.Refusal, match='transport'):
        collect()


def test_verified_compilation_rejects_ambiguous_source_and_bypasses_pyc(tmp_path):
    # A separate interpreter keeps global loader changes out of pytest.
    driver = tmp_path / 'check.py'
    driver.write_text('''import importlib.util, sys, pathlib, py_compile, os, types, shutil
spec = importlib.util.spec_from_file_location('launch', sys.argv[1])
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)
repo = pathlib.Path(sys.argv[2])
repo.mkdir()
source = repo / 'probe.py'
source.write_text('value = "STALE"\\n')
sys.pycache_prefix = None
py_compile.compile(str(source), doraise=True)
st = source.stat()
source.write_text('value = "FRESH"\\n')
os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns))
files = [{'path': 'probe.py', 'sha256': launch.digest(source.read_bytes())}]
stale = types.ModuleType('probe')
stale.__file__ = str(source)
stale.value = 'STALE'
sys.modules['probe'] = stale
try:
    launch.VerifiedSource(repo, files)
except launch.LaunchRefusal as e:
    assert 'predates' in str(e)
else:
    raise AssertionError('preloaded source admitted')
del sys.modules['probe']
allowed_native = repo.parent / 'allowed.so'
unreviewed_native = repo.parent / 'unreviewed.so'
drift_native = repo.parent / 'drift.so'
for path in (allowed_native, unreviewed_native, drift_native):
    shutil.copyfile(sys.argv[3], path)
native_hash = launch.digest(allowed_native.read_bytes())
verified = launch.VerifiedSource(repo, files, {'files': {str(allowed_native): native_hash, str(drift_native): native_hash}, 'observer_sha256': 'unused'})
try:
    native_spec = importlib.util.spec_from_file_location('_psutil_linux', unreviewed_native)
    importlib.util.module_from_spec(native_spec)
except launch.LaunchRefusal as e:
    assert 'unreviewed native' in str(e)
else:
    raise AssertionError('unreviewed native initialized')
native_spec = importlib.util.spec_from_file_location('_psutil_linux', allowed_native)
assert importlib.util.module_from_spec(native_spec).__name__ == '_psutil_linux'
drift_native.write_bytes(b'changed')
native_spec = importlib.util.spec_from_file_location('_psutil_linux', drift_native)
try:
    importlib.util.module_from_spec(native_spec)
except launch.LaunchRefusal as e:
    assert 'drift' in str(e)
else:
    raise AssertionError('native dependency drift admitted')
sys.path.insert(0, str(repo))
import probe
assert probe.value == 'FRESH'
assert verified.loaded['probe']['sha256'] == files[0]['sha256']
source.write_text('value = "DRIFT"\\n')
try:
    importlib.reload(probe)
except launch.LaunchRefusal as e:
    assert 'drift' in str(e)
else:
    raise AssertionError('drift admitted')
(repo / 'untracked.py').write_text('value = 1\\n')
try:
    import untracked
except launch.LaunchRefusal as e:
    assert 'untracked' in str(e)
else:
    raise AssertionError('untracked source admitted')
other = repo.parent / 'other'
other.mkdir()
(other / 'probe.py').write_text('value = "WRONG"\\n')
sys.path.insert(0, str(other))
del sys.modules['probe']
try:
    import probe
except launch.LaunchRefusal as e:
    assert 'outside' in str(e)
else:
    raise AssertionError('shadow source admitted')
(other / 'unreviewed.py').write_text('value = 1\\n')
try:
    import unreviewed
except launch.LaunchRefusal as e:
    assert 'unreviewed import' in str(e)
else:
    raise AssertionError('unreviewed dependency admitted')
''')
    import psutil._psutil_linux as native
    check = subprocess.run([sys.executable, '-I', '-S', '-B', '-X', 'pycache_prefix=/dev/null', str(driver), str(SCRIPTS / 'launch_gateway.py'),
                            str(tmp_path / 'repo'), str(native.__file__)], capture_output=True, text=True, timeout=15)
    assert check.returncode == 0, check.stderr


@pytest.mark.parametrize('interpreter', list(dict.fromkeys([sys.executable, sys._base_executable])))
def test_launcher_rejects_site_startup_and_dependency_drift(tmp_path, interpreter):
    # No environment creation: use the existing interpreter. A private import
    # directory demonstrates that .pth and sitecustomize are never evaluated.
    deps = tmp_path / 'imports'
    deps.mkdir()
    marker = tmp_path / 'poison-executed'
    poison = f"open({str(marker)!r}, 'w').write('unsafe')\n"
    (deps / 'sitecustomize.py').write_text(poison)
    (deps / 'unsafe.pth').write_text('import sitecustomize\n')
    driver = tmp_path / 'isolate.py'
    driver.write_text('''import importlib.util, sys, pathlib
spec = importlib.util.spec_from_file_location('launch', sys.argv[1])
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)
paths = [str(pathlib.Path(p).resolve()) for p in sys.path if pathlib.Path(p).is_dir()]
paths.append(sys.argv[2])
venv_path = pathlib.Path(sys.executable).parent.parent.resolve() / 'pyvenv.cfg'
venv_config = str(venv_path) if venv_path.is_file() else ''
original_prefix = sys.prefix
value = {'paths': paths, 'venv_config': venv_config, 'files': launch.dependency_files(paths, venv_config), 'observer_sha256': 'unused'}
path = pathlib.Path(sys.argv[3])
launch.durable(path, value)
sha = launch.digest(path.read_bytes())
launch.isolate(path.parent / 'repo', path, sha)
assert sys.prefix == (str(pathlib.Path(venv_config).parent) if venv_config else original_prefix)
assert 'sitecustomize' not in sys.modules
assert not pathlib.Path(sys.argv[4]).exists()
(pathlib.Path(sys.argv[2]) / 'sitecustomize.py').write_text('changed')
try:
    launch.isolate(path.parent / 'repo', path, sha)
except launch.LaunchRefusal as e:
    assert 'drift' in str(e)
else:
    raise AssertionError('dependency drift admitted')
''')
    env = {'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(deps),
           'PYTHONHOME': str(deps), 'PYTHONUSERBASE': str(deps)}
    check = subprocess.run([interpreter, '-I', '-S', '-B', '-X', 'pycache_prefix=/dev/null', str(driver), str(SCRIPTS / 'launch_gateway.py'),
                            str(deps), str(tmp_path / 'deps.json'), str(marker)],
                           env=env, capture_output=True, text=True, timeout=30)
    assert check.returncode == 0, check.stderr
    # Without -S, production launcher must refuse even if no source is imported.
    check = subprocess.run([interpreter, '-I', str(SCRIPTS / 'launch_gateway.py'),
                            '--repo', str(deps), '--startup-json', str(tmp_path / 'startup.json'),
                            '--dependencies', str(tmp_path / 'deps.json'), '--dependencies-sha256', 'unused',
                            '--', '-m', 'hermes_cli.main', 'gateway', 'run'],
                           capture_output=True, text=True, timeout=10)
    assert check.returncode == 2 and 'Python -I -S required' in check.stderr


@pytest.mark.parametrize('actual_gateway', ['9ccb53e3'], indirect=True)
def test_rollback_final_launch_real_runner_collect_drain(actual_gateway, monkeypatch):
    test_final_launch_real_runner_collect_drain(actual_gateway, monkeypatch)


def test_health_entrypoint_waits_for_real_startup(actual_gateway, monkeypatch, capsys):
    root, home, secondary, startup, process = actual_gateway
    monkeypatch.setattr(health_contract, 'show', lambda unit: {'ActiveState': 'active', 'MainPID': str(process.pid)})
    monkeypatch.setattr(sys, 'argv', ['runtime_health.py', str(startup), str(root / 'live.json'),
                                    'disposable.service', str(home)])
    assert not startup.exists()
    assert health.main() == 0, (root / 'gateway.log').read_text()[-6000:]
    assert json.loads(capsys.readouterr().out)['healthy'] is True


def test_webhook_observer_uses_real_listener(tmp_path, monkeypatch):
    import asyncio
    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))

    async def exercise():
        adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={'host': '127.0.0.1', 'port': 0}))
        with pytest.raises(RuntimeError):
            await observation.connected(adapter)
        try:
            assert await adapter.connect()
            await observation.connected(adapter)
            sites = tuple(adapter._runner.sites)
            for site in sites:
                site._server.close()
                await site._server.wait_closed()
            assert adapter.is_connected  # transition flag is now stale
            with pytest.raises(RuntimeError, match='webhook listener'):
                await observation.connected(adapter)
        finally:
            await adapter.disconnect()
        with pytest.raises(RuntimeError):
            await observation.connected(adapter)
    asyncio.run(exercise())


def test_feishu_observer_uses_websocket_transport_primitives(tmp_path, monkeypatch):
    import asyncio
    import threading
    from types import SimpleNamespace
    from gateway.config import PlatformConfig
    from plugins.platforms.feishu.adapter import FeishuAdapter
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))

    async def exercise():
        adapter = FeishuAdapter(PlatformConfig(enabled=True, extra={
            'app_id': 'cli_test',
            'app_secret': 'secret_test',
            'connection_mode': 'websocket',
        }))
        adapter._mark_connected()
        with pytest.raises(RuntimeError, match='feishu event handler'):
            await observation.connected(adapter)
        adapter._event_handler = object()
        with pytest.raises(RuntimeError, match='feishu websocket transport'):
            await observation.connected(adapter)

        ws_loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run_loop():
            asyncio.set_event_loop(ws_loop)
            ready.set()
            ws_loop.run_forever()

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()
        assert ready.wait(timeout=2.0)
        ws_future = asyncio.get_running_loop().create_future()
        adapter._ws_client = SimpleNamespace()
        adapter._ws_thread_loop = ws_loop
        adapter._ws_future = ws_future
        try:
            await observation.connected(adapter)

            adapter._event_handler = None
            with pytest.raises(RuntimeError, match='feishu event handler'):
                await observation.connected(adapter)
            adapter._event_handler = object()

            adapter._ws_client = None
            with pytest.raises(RuntimeError, match='feishu websocket transport'):
                await observation.connected(adapter)
            adapter._ws_client = SimpleNamespace()

            ws_future.set_result(None)
            with pytest.raises(RuntimeError, match='feishu websocket transport stale'):
                await observation.connected(adapter)
            adapter._ws_future = asyncio.get_running_loop().create_future()

            ws_loop.call_soon_threadsafe(ws_loop.stop)
            thread.join(timeout=2.0)
            with pytest.raises(RuntimeError, match='feishu websocket loop'):
                await observation.connected(adapter)
            ws_loop.close()
            with pytest.raises(RuntimeError, match='feishu websocket loop closed'):
                await observation.connected(adapter)
        finally:
            if not ws_future.done():
                ws_future.cancel()
            future = getattr(adapter, '_ws_future', None)
            if future is not None and not future.done():
                future.cancel()
            if not ws_loop.is_closed():
                ws_loop.call_soon_threadsafe(ws_loop.stop)
                thread.join(timeout=2.0)
                ws_loop.close()

    asyncio.run(exercise())


def test_required_feishu_websocket_collects_and_refuses_stale_transport(observation_runtime, tmp_path):
    import asyncio
    import threading
    from types import SimpleNamespace
    from gateway.config import Platform, PlatformConfig
    from plugins.platforms.feishu.adapter import FeishuAdapter
    runner, homes, started, base, collect = observation_runtime
    homes.pop('secondary')
    runner._profile_adapters.clear()
    base['served_profiles'] = ['default']
    runner._release_required_platforms = {'default': frozenset({'feishu'})}

    async def exercise():
        adapter = FeishuAdapter(PlatformConfig(enabled=True, extra={
            'app_id': 'cli_test',
            'app_secret': 'secret_test',
            'connection_mode': 'websocket',
        }))
        adapter._mark_connected()
        ws_loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run_loop():
            asyncio.set_event_loop(ws_loop)
            ready.set()
            ws_loop.run_forever()

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()
        assert ready.wait(timeout=2.0)
        adapter._ws_client = SimpleNamespace()
        adapter._ws_thread_loop = ws_loop
        adapter._ws_future = asyncio.get_running_loop().create_future()
        adapter._event_handler = object()
        runner.adapters[Platform.FEISHU] = adapter
        try:
            assert collect(await observation.observe(runner, homes, 'default', started))['healthy']
            adapter._ws_future.set_result(None)
            with pytest.raises(c.Refusal, match='transport'):
                collect(await observation.observe(runner, homes, 'default', started))
        finally:
            future = getattr(adapter, '_ws_future', None)
            if future is not None and not future.done():
                future.cancel()
            if not ws_loop.is_closed():
                ws_loop.call_soon_threadsafe(ws_loop.stop)
                thread.join(timeout=2.0)
                ws_loop.close()

    asyncio.run(exercise())


def test_feishu_observer_uses_real_webhook_listener(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from gateway.config import PlatformConfig
    import plugins.platforms.feishu.adapter as feishu_mod
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    if not feishu_mod.FEISHU_WEBHOOK_AVAILABLE or feishu_mod.web is None:
        pytest.skip('aiohttp unavailable for real Feishu webhook listener')

    async def exercise():
        adapter = feishu_mod.FeishuAdapter(PlatformConfig(enabled=True, extra={
            'app_id': 'cli_test',
            'app_secret': 'secret_test',
            'connection_mode': 'webhook',
            'verification_token': 'verify_test',
            'webhook_host': '127.0.0.1',
            'webhook_port': 0,
        }))
        with pytest.raises(RuntimeError, match='adapter disconnected'):
            await observation.connected(adapter)
        with (
            patch.object(adapter, '_build_lark_client', return_value=SimpleNamespace()),
            patch.object(adapter, '_build_event_handler', return_value=object()),
            patch.object(adapter, '_hydrate_bot_identity', new=AsyncMock()),
        ):
            try:
                await adapter._connect_webhook()
                adapter._mark_connected()
                await observation.connected(adapter)
                site = adapter._webhook_site
                site._server.close()
                await site._server.wait_closed()
                assert adapter.is_connected
                with pytest.raises(RuntimeError, match='feishu webhook listener'):
                    await observation.connected(adapter)
            finally:
                await adapter.disconnect()
        with pytest.raises(RuntimeError):
            await observation.connected(adapter)

    asyncio.run(exercise())


def test_observer_lazy_sessions_and_pending_warmup(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway import control_socket
    from hermes_state import SessionDB
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(control_socket, 'build_status_payload', lambda: {})
    secondary = tmp_path / 'profiles/secondary'
    secondary.mkdir(parents=True)
    db = SessionDB(db_path=secondary / 'state.db')
    db.close()
    import hermes_state
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', tmp_path / 'state.db')
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(adapters={}, _profile_adapters={}, session_store=store,
                             _background_tasks=set(), _running_agent_count=lambda: 0,
                             _draining=False, _external_drain_active=False, _running=True,
                             _startup_restore_in_progress=False, _startup_warmup_task=None,
                             _release_required_platforms={'default': frozenset(), 'secondary': frozenset()},
                             _release_adapter_owners=set(), _failed_platforms={}, _profile_failed_platforms={})

    async def exercise():
        homes = {'default': tmp_path, 'secondary': secondary}
        before = set(store._db_handles)
        status = await observation.observe(runner, homes, 'default', time.time() - 1)
        assert status['release_observation']['profiles']['secondary']['sessions'] is True
        assert set(store._db_handles) == before
        task = asyncio.create_task(asyncio.sleep(30))
        runner._startup_warmup_task = task
        try:
            status = await observation.observe(runner, homes, 'default', time.time() - 1)
            assert status['release_observation']['work']['background'] is True
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        status = await observation.observe(runner, homes, 'default', time.time() - 1)
        assert status['release_observation']['work']['background'] is False
        (secondary / 'state.db').unlink()
        with pytest.raises(Exception):
            await observation.observe(runner, homes, 'default', time.time() - 1)
        assert not (secondary / 'state.db').exists()
    try:
        asyncio.run(exercise())
    finally:
        store.close_all_db_handles()


def test_health_entrypoint_bounded_refusal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['runtime_health.py', str(tmp_path / 'startup.json'),
                                    str(tmp_path / 'live.json'), 'disposable.service', str(tmp_path),
                                    '--startup-timeout', '0'])
    assert health.main() == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert 'runtime health refused' in captured.err


@pytest.fixture
def observation_runtime(tmp_path, monkeypatch):
    import weakref
    from types import SimpleNamespace
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway import control_socket
    from hermes_state import SessionDB
    from cron.jobs import record_ticker_heartbeat
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    homes = {'default': tmp_path, 'secondary': tmp_path / 'profiles/secondary'}
    started = time.time() - 1
    for home in homes.values():
        home.mkdir(parents=True, exist_ok=True)
        db = SessionDB(db_path=home / 'state.db')
        db.close()
        token = set_hermes_home_override(home)
        try:
            record_ticker_heartbeat(success=True)
        finally:
            reset_hermes_home_override(token)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    import hermes_state
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', tmp_path / 'state.db')
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(adapters={}, _profile_adapters={'secondary': {}}, session_store=store,
                             _background_tasks=set(), _running_agent_count=lambda: 0,
                             _draining=False, _external_drain_active=False, _running=True,
                             _startup_restore_in_progress=False, _startup_warmup_task=None,
                             _release_required_platforms={}, _release_adapter_owners=weakref.WeakSet(),
                             _failed_platforms={}, _profile_failed_platforms={})
    base = {'pid': os.getpid(), 'answering_pid': os.getpid(), 'start_time': '1',
            'gateway_state': 'running', 'served_profiles': list(homes)}
    monkeypatch.setattr(control_socket, 'build_status_payload', lambda: dict(base))
    # Keep unchanged OS/provenance validation outside these component tests;
    # collect itself still evaluates actual observer, SQLite and ticker data.
    monkeypatch.setattr(health, '_read_startup', lambda *a: {})
    monkeypatch.setattr(health, 'read_health', lambda *a: {'healthy': True})
    def collect(status):
        monkeypatch.setattr(health, '_query_socket', lambda home, verb: status)
        return health.collect(tmp_path / 'startup.json', tmp_path / 'live.json', 'fixture.service', tmp_path, 90)
    try:
        yield runner, homes, started, base, collect
    finally:
        store.close_all_db_handles()


@pytest.mark.parametrize('profile', ['default', 'secondary'])
def test_required_adapter_missing_or_failed_blocks_collect(observation_runtime, monkeypatch, profile):
    import asyncio
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter
    from gateway.platforms.api_server import APIServerAdapter
    from aiohttp import web
    runner, homes, started, base, collect = observation_runtime

    async def exercise():
        adapters = []
        try:
            for name, home in homes.items():
                monkeypatch.setenv('HERMES_HOME', str(home))
                adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={'host': '127.0.0.1', 'port': 0}))
                assert await adapter.connect()
                adapters.append(adapter)
                group = runner.adapters if name == 'default' else runner._profile_adapters[name]
                group[Platform.WEBHOOK] = adapter
                survivor = APIServerAdapter(PlatformConfig(enabled=True))
                survivor._runner = web.AppRunner(web.Application())
                await survivor._runner.setup()
                survivor._site = web.TCPSite(survivor._runner, '127.0.0.1', 0)
                await survivor._site.start()
                survivor._mark_connected()
                adapters.append(survivor)
                group[Platform.API_SERVER] = survivor
                cfg = GatewayConfig(platforms={Platform.WEBHOOK: PlatformConfig(enabled=True),
                                               Platform.API_SERVER: PlatformConfig(enabled=True)})
                runner._release_required_platforms[name] = observation.required_platforms(cfg)
            assert collect(await observation.observe(runner, homes, 'default', started))['healthy']
            group = runner.adapters if profile == 'default' else runner._profile_adapters[profile]
            # A surviving real listener cannot hide another required endpoint.
            expected = runner._release_required_platforms[profile]
            runner._release_required_platforms[profile] = expected | frozenset({'telegram'})
            with pytest.raises(c.Refusal, match='transport'):
                collect(await observation.observe(runner, homes, 'default', started))
            runner._release_required_platforms[profile] = expected
            failed = runner._failed_platforms if profile == 'default' else runner._profile_failed_platforms.setdefault(profile, {})
            failed[Platform.WEBHOOK] = object()
            with pytest.raises(c.Refusal, match='transport'):
                collect(await observation.observe(runner, homes, 'default', started))
            failed.clear()
            saved = group.pop(Platform.WEBHOOK)
            with pytest.raises(c.Refusal, match='transport'):
                collect(await observation.observe(runner, homes, 'default', started))
            group[Platform.WEBHOOK] = saved
            assert collect(await observation.observe(runner, homes, 'default', started))['healthy']
            runner._release_required_platforms.pop(profile)
            with pytest.raises(RuntimeError, match='required profile'):
                await observation.observe(runner, homes, 'default', started)
        finally:
            for adapter in adapters:
                await adapter.disconnect()
    asyncio.run(exercise())


@pytest.mark.parametrize('profile', ['default', 'secondary'])
def test_post_handler_delivery_blocks_drain(observation_runtime, monkeypatch, tmp_path, profile):
    import asyncio
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter
    from gateway.platforms.base import MessageEvent, SendResult
    from gateway.session import SessionSource
    runner, homes, started, base, collect = observation_runtime
    base['gateway_state'] = 'draining'
    runner._draining = True
    runner._release_required_platforms = {name: frozenset() for name in homes}
    deadline = int(time.time()) + 240
    hold = tmp_path / 'hold.json'
    c.durable(hold, {'kind': 'release-admission-hold', 'repo': str(tmp_path), 'unit': 'fixture.service',
                    'pid': os.getpid(), 'starttime': '1', 'observed': int(time.time()),
                    'valid_until': deadline, 'recovery_deadline': deadline,
                    'owner': 'isolated test', 'recovery_owner': 'isolated test', 'approved': True,
                    'coverage': {key: 'isolated test consumer hold' for key in drain.COVERAGE}})
    def proof(status):
        monkeypatch.setattr(drain, '_query_socket', lambda *a: status)
        return drain.prove(tmp_path, hold, str(tmp_path), 'fixture.service', os.getpid(), '1',
                           max_age=90, repeat_delay=0, recovery_deadline=deadline)

    async def exercise():
        sending = asyncio.Event()
        release = asyncio.Event()
        returned = asyncio.Event()
        class DeliveryAdapter(WebhookAdapter):
            async def send(self, *args, **kwargs):
                sending.set()
                await release.wait()
                return SendResult(success=True, message_id='local')
        adapter = DeliveryAdapter(PlatformConfig(enabled=True, typing_indicator=False))
        async def handler(event):
            returned.set()
            return 'local final response'
        adapter.set_message_handler(handler)
        group = runner.adapters if profile == 'default' else runner._profile_adapters[profile]
        group[Platform.WEBHOOK] = adapter
        runner._release_adapter_owners.add(adapter)
        event = MessageEvent(text='/fixture', source=SessionSource(platform=Platform.WEBHOOK, chat_id='local'))
        assert adapter._start_session_processing(event, 'fixture-session')
        task = adapter._session_tasks['fixture-session']
        try:
            await asyncio.wait_for(sending.wait(), timeout=10)
            assert returned.is_set() and not task.done()
            status = await observation.observe(runner, homes, 'default', started)
            assert status['release_observation']['work']['turns'] == 0
            with pytest.raises(c.Refusal, match='background'):
                proof(status)
            # Runtime removal must not hide an old adapter's delivery task.
            group.clear()
            with pytest.raises(c.Refusal, match='background'):
                proof(await observation.observe(runner, homes, 'default', started))
        finally:
            release.set()
            await asyncio.wait_for(task, timeout=10)
        assert proof(await observation.observe(runner, homes, 'default', started))['active_jobs'] == 0
        adapter._active_sessions['orphan'] = asyncio.Event()
        with pytest.raises(c.Refusal, match='background'):
            proof(await observation.observe(runner, homes, 'default', started))
        adapter._active_sessions.clear()
        background = asyncio.create_task(asyncio.sleep(30))
        adapter._background_tasks.add(background)
        try:
            with pytest.raises(c.Refusal, match='background'):
                proof(await observation.observe(runner, homes, 'default', started))
        finally:
            background.cancel()
            await asyncio.gather(background, return_exceptions=True)
    asyncio.run(exercise())


def test_install_captures_consumed_profile_config(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from gateway import config as config_module, control_socket
    from gateway.platforms.webhook import WebhookAdapter
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    secondary = tmp_path / 'profiles/secondary'
    secondary.mkdir(parents=True)
    (tmp_path / 'config.yaml').write_text('platforms:\n  webhook:\n    enabled: true\n', encoding='utf-8')
    (secondary / 'config.yaml').write_text('platforms:\n  telegram:\n    enabled: true\n', encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    class Runtime:
        def __init__(self):
            self.config = config_module.load_gateway_config()
            self.config.multiplex_profiles = True
        def _create_adapter(self, platform, config):
            return WebhookAdapter(config)
        async def _start_one_profile_adapters(self, profile_name):
            token = set_hermes_home_override(secondary)
            try:
                return config_module.load_gateway_config()
            finally:
                reset_hermes_home_override(token)
        _run_secondary_profile_reconnect = _start_one_profile_adapters
    gateway = SimpleNamespace(GatewayRunner=Runtime,
                              _multiplex_profile_homes=lambda cfg: [('default', tmp_path), ('secondary', secondary)])
    # Restore global composition seams when this small test finishes.
    monkeypatch.setattr(config_module, 'load_gateway_config', config_module.load_gateway_config)
    monkeypatch.setattr(control_socket.GatewayControlServer, '__init__', control_socket.GatewayControlServer.__init__)
    observation.install(gateway, lambda: None)
    runner = Runtime()
    assert 'webhook' in runner._release_required_platforms['default']
    assert 'secondary' not in runner._release_required_platforms
    token = set_hermes_home_override(secondary)
    try:
        consumed = config_module.load_gateway_config()
    finally:
        reset_hermes_home_override(token)
    assert 'secondary' not in runner._release_required_platforms  # unrelated read is not adoption
    import asyncio
    consumed = asyncio.run(runner._start_one_profile_adapters('secondary'))
    assert runner._release_required_platforms['secondary'] == observation.required_platforms(consumed)
    assert 'telegram' in runner._release_required_platforms['secondary']
    adapter = runner._create_adapter(config_module.Platform.WEBHOOK, config_module.PlatformConfig())
    assert adapter in runner._release_adapter_owners
    consumed.platforms[config_module.Platform.TELEGRAM].enabled = None
    with pytest.raises(RuntimeError, match='enabled state'):
        observation.required_platforms(consumed)


def test_actual_api_startup_maintenance_preserves_idle_and_busy(observation_runtime, monkeypatch, tmp_path):
    import asyncio
    import secrets
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    runner, homes, started, base, collect = observation_runtime
    # One actual API listener on the root profile, as in production.
    homes.pop('secondary')
    runner._profile_adapters.clear()
    base['served_profiles'] = ['default']
    runner._release_required_platforms = {'default': frozenset({'api_server'})}
    deadline = int(time.time()) + 240
    hold = tmp_path / 'hold.json'
    c.durable(hold, {'kind': 'release-admission-hold', 'repo': str(tmp_path), 'unit': 'fixture.service',
                    'pid': os.getpid(), 'starttime': '1', 'observed': int(time.time()),
                    'valid_until': deadline, 'recovery_deadline': deadline,
                    'owner': 'isolated test', 'recovery_owner': 'isolated test', 'approved': True,
                    'coverage': {key: 'isolated test consumer hold' for key in drain.COVERAGE}})
    def proof(status):
        monkeypatch.setattr(drain, '_query_socket', lambda *a: status)
        return drain.prove(tmp_path, hold, str(tmp_path), 'fixture.service', os.getpid(), '1',
                           max_age=90, repeat_delay=0, recovery_deadline=deadline)

    async def exercise():
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={
            'host': '127.0.0.1', 'port': 0, 'key': secrets.token_hex(32)}))
        runner.adapters[Platform.API_SERVER] = adapter
        runner._release_adapter_owners.add(adapter)
        try:
            # Unmodified startup: real routes, auth guard, listener and the
            # permanent maintenance producer are all constructed here.
            assert await adapter.connect()
            await asyncio.sleep(0)
            sweep, = [task for task in adapter._background_tasks if not task.done()]
            assert observation.api_maintenance(adapter, sweep)
            assert not observation.adapter_busy(adapter)
            assert collect(await observation.observe(runner, homes, 'default', started))['healthy']
            base['gateway_state'] = 'draining'
            runner._draining = True
            assert proof(await observation.observe(runner, homes, 'default', started))['active_jobs'] == 0
            assert not sweep.done()  # idle passes with actual maintenance still alive
            for field in ('_pending_agent_requests', '_inflight_agent_runs'):
                setattr(adapter, field, 1)
                try:
                    with pytest.raises(c.Refusal, match='api'):
                        proof(await observation.observe(runner, homes, 'default', started))
                finally:
                    setattr(adapter, field, 0)
            run = asyncio.create_task(asyncio.sleep(30))
            adapter._active_run_tasks['fixture'] = run
            try:
                with pytest.raises(c.Refusal, match='api'):
                    proof(await observation.observe(runner, homes, 'default', started))
            finally:
                adapter._active_run_tasks.clear()
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)
            # Neither the same name nor the generic watcher tag exempts work.
            async def _sweep_orphaned_runs():
                await asyncio.sleep(30)
            impostor = asyncio.create_task(_sweep_orphaned_runs(), name='_sweep_orphaned_runs')
            impostor._hermes_supervised_watcher = True
            # Same native function with another owner is also not maintenance
            # belonging to this adapter.
            foreign = asyncio.create_task(APIServerAdapter._sweep_orphaned_runs(object()))
            try:
                for task in (impostor, foreign):
                    adapter._background_tasks.add(task)
                    assert not observation.api_maintenance(adapter, task)
                    with pytest.raises(c.Refusal, match='background'):
                        proof(await observation.observe(runner, homes, 'default', started))
                    adapter._background_tasks.discard(task)
            finally:
                for task in (impostor, foreign):
                    task.cancel()
                await asyncio.gather(impostor, foreign, return_exceptions=True)
            adapter._session_tasks['guarded'] = sweep
            with pytest.raises(c.Refusal, match='background'):
                proof(await observation.observe(runner, homes, 'default', started))
            adapter._session_tasks.clear()
            assert proof(await observation.observe(runner, homes, 'default', started))['active_jobs'] == 0
        finally:
            await adapter.cancel_background_tasks()
            await adapter.disconnect()
    asyncio.run(exercise())
