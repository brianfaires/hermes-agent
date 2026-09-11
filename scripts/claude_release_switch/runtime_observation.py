"""Installed read-only composition of the gateway's existing status callback.

Runs observations on the owning event loop. No network probes, admission changes,
or persisted platform timestamp writes. Unknown/missing primitives fail closed.
"""
import asyncio
from pathlib import Path
import time
import sqlite3
import weakref
from contextvars import ContextVar


def check(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def count(value):
    check(type(value) is int and value >= 0, 'unreadable work count')
    return value


async def connected(adapter):
    check(adapter.is_connected is True and not adapter.has_fatal_error, 'adapter disconnected')
    kind = adapter.platform.value
    if kind == 'discord':
        ok, _reason = adapter._read_websocket_health(adapter._client)
        check(ok is True, 'discord transport unhealthy')
    elif kind == 'telegram':
        check(not adapter._webhook_mode, 'telegram webhook observation unsupported')
        check(adapter._app.running and adapter._app.updater.running, 'telegram poller stopped')
        stamp = adapter._polling_last_progress_monotonic
        check(type(stamp) in (int, float) and 0 <= time.monotonic() - stamp <= 90,
              'telegram getUpdates progress stale')
    elif kind == 'slack':
        check(await adapter._socket_transport_connected() is True, 'slack transport unavailable')
        check(not adapter._socket_ping_pong_stale(), 'slack transport stale')
    elif kind == 'webhook':
        sites = adapter._runner.sites if adapter._runner is not None else ()
        check(bool(sites) and all(site._server is not None and site._server.is_serving()
                                  for site in sites), 'webhook listener unavailable')
    elif kind == 'api_server':
        check(adapter._site is not None and adapter._site._server.is_serving(), 'API listener unavailable')
    else:
        raise RuntimeError('unsupported adapter observation: ' + kind)


def check_session_tables(conn):
    for table in ('sessions', 'messages'):
        conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone()


def required_platforms(config):
    check(type(config.platforms) is dict, 'required platforms unreadable')
    required = set()
    for platform, value in config.platforms.items():
        check(type(value.enabled) is bool, 'required enabled state unreadable')
        if value.enabled:
            check(isinstance(platform.value, str) and platform.value, 'required platform unreadable')
            required.add(platform.value)
    return frozenset(required)


def api_maintenance(adapter, task):
    """Recognize only this native API adapter's permanent orphan sweeper."""
    if adapter.platform.value != 'api_server':
        return False
    from gateway.platforms.api_server import APIServerAdapter
    if type(adapter) is not APIServerAdapter:
        return False
    native = APIServerAdapter._sweep_orphaned_runs
    if getattr(adapter._sweep_orphaned_runs, '__func__', None) is not native:
        return False
    coro = task.get_coro()
    frame = getattr(coro, 'cr_frame', None)
    return (getattr(coro, 'cr_code', None) is native.__code__
            and frame is not None and frame.f_locals.get('self') is adapter)


def adapter_busy(adapter):
    # BasePlatformAdapter owns these message-processing registries. A session
    # task remains here through post-handler TTS/media/final-response delivery.
    check(type(adapter._session_tasks) is dict and type(adapter._active_sessions) is dict
          and type(adapter._background_tasks) is set, 'adapter work unreadable')
    return (bool(adapter._active_sessions)
            or any(not task.done() for task in adapter._session_tasks.values())
            or any(not task.done() and not api_maintenance(adapter, task)
                   for task in adapter._background_tasks))


def cron_running_count(scheduler):
    """Read the current global keys, or the admitted older global-ID snapshot."""
    absent = object()
    getter = getattr(scheduler, 'get_running_job_keys', absent)
    legacy = getter is absent
    if legacy:
        getter = getattr(scheduler, 'get_running_job_ids', None)
    check(callable(getter), 'cron running snapshot unavailable')
    jobs = getter()  # Errors must refuse, never fall back to an idle snapshot.
    check(type(jobs) is frozenset, 'cron running snapshot unreadable')
    if legacy:
        check(all(type(job) is str for job in jobs), 'cron legacy IDs unreadable')
    else:
        check(all(type(job) is tuple and len(job) == 2
                  and isinstance(job[0], Path) and type(job[1]) is str for job in jobs),
              'cron running keys unreadable')
    return len(jobs)


async def observe(runner, homes, primary, started):
    from cron import scheduler
    from tools.async_delegation import active_count
    from tools.process_registry import process_registry
    from gateway.control_socket import build_status_payload

    status = build_status_payload()
    groups = {primary: runner.adapters, **runner._profile_adapters}
    check(set(groups) <= set(homes), 'unknown adapter profile')
    required = runner._release_required_platforms
    check(type(required) is dict and set(required) == set(homes), 'required profile state unreadable')
    failures = {primary: runner._failed_platforms, **runner._profile_failed_platforms}
    check(set(failures) <= set(homes), 'unknown failed profile')
    owned = set(runner._release_adapter_owners)
    for adapters in groups.values():
        check(type(adapters) is dict, 'adapter state unreadable')
        owned.update(adapters.values())
    adapter_work = any(adapter_busy(adapter) for adapter in owned)
    cron = cron_running_count(scheduler)
    api = 0
    profiles = {}
    for name, home in homes.items():
        adapters = groups.get(name, {})
        expected = required[name]
        check(type(expected) is frozenset and all(type(p) is str for p in expected), 'required platforms unreadable')
        failed = failures.get(name, {})
        check(type(failed) is dict, 'failed platform state unreadable')
        platform_states = {platform: False for platform in expected}
        for platform, adapter in adapters.items():
            if platform.value == 'api_server':
                # The adapter helper catches exceptions and returns zero. Read
                # the same producer fields strictly so missing state is busy.
                api += count(adapter._pending_agent_requests) + count(adapter._inflight_agent_runs)
                api += sum(not task.done() for task in adapter._active_run_tasks.values())
            try:
                await connected(adapter)
                platform_states[platform.value] = platform not in failed
            except Exception:
                platform_states[platform.value] = False
        # These are the actual handles used by the multiplex SessionStore,
        # not a root-only aggregate and not a new write-capable connection.
        store = runner.session_store
        with store._db_handles_lock:
            handle = store._db_handles.get(home / 'state.db')
            if handle is not None:
                check_session_tables(handle._conn)
            else:
                # Secondary handles open lazily on their first inbound turn.
                # Check the existing database without manufacturing a session
                # or installing a new write-capable handle in the runner.
                conn = sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True, timeout=.2)
                try:
                    check_session_tables(conn)
                finally:
                    conn.close()
        profiles[name] = {'home': str(home), 'platforms': platform_states, 'sessions': True}
    warmup = getattr(runner, '_startup_warmup_task', None)
    background = bool(warmup is not None and not warmup.done()) or any(not task.done() and not getattr(task, '_hermes_supervised_watcher', False)
                     for task in runner._background_tasks)
    with process_registry._lock:
        # Snapshot only; has_any_active() refreshes detached sessions.
        processes = any(not session.exited for session in process_registry._running.values())
        watchers = bool(process_registry.pending_watchers)
    background = bool(background or adapter_work or count(active_count()) or processes or watchers)
    work = {'turns': count(runner._running_agent_count()), 'cron': cron, 'api': api,
            'background': background}
    status['release_observation'] = {
        'observed': time.time(), 'started': started, 'profiles': profiles, 'work': work,
        'draining': bool(runner._draining or runner._external_drain_active),
        'running': runner._running is True and not runner._startup_restore_in_progress,
    }
    return status


def install(gateway, attest):
    """Use existing constructors/callback injection on candidate and rollback."""
    from gateway import control_socket, config as config_module
    from hermes_constants import get_hermes_home
    from gateway.status import _get_process_hermes_home, _profile_label_for_home

    started = time.time()
    owners = []
    consuming_profile = ContextVar('release_consuming_profile', default=None)
    original_runner_init = gateway.GatewayRunner.__init__
    original_server_init = control_socket.GatewayControlServer.__init__
    original_load_config = config_module.load_gateway_config
    original_create_adapter = gateway.GatewayRunner._create_adapter

    def runner_init(self, *args, **kwargs):
        original_runner_init(self, *args, **kwargs)
        home = _get_process_hermes_home().resolve()
        primary = _profile_label_for_home(str(home))
        homes = dict(gateway._multiplex_profile_homes(self.config)) if self.config.multiplex_profiles else {primary: home}
        homes = {name: Path(path).resolve() for name, path in homes.items()}
        check(homes.get(primary) == home, 'primary profile home mismatch')
        self._release_required_platforms = {primary: required_platforms(self.config)}
        self._release_adapter_owners = weakref.WeakSet()
        owners.append((self, homes, primary))

    def load_config(*args, **kwargs):
        config = original_load_config(*args, **kwargs)
        consumer = consuming_profile.get()
        if consumer is not None:
            runner, name = consumer
            home = get_hermes_home().resolve()
            owner = next(item for item in owners if item[0] is runner)
            check(owner[1].get(name) == home, 'config consumer home mismatch')
            runner._release_required_platforms[name] = required_platforms(config)
        return config

    def create_adapter(self, *args, **kwargs):
        adapter = original_create_adapter(self, *args, **kwargs)
        if adapter is not None:
            self._release_adapter_owners.add(adapter)
        return adapter

    def server_init(self, *args, **kwargs):
        check(len(owners) == 1, 'ambiguous gateway owner')
        runner, homes, primary = owners[0]
        loop = asyncio.get_running_loop()

        def status():
            future = asyncio.run_coroutine_threadsafe(observe(runner, homes, primary, started), loop)
            try:
                value = future.result(timeout=2)
                attest()
                return value
            finally:
                if not future.done():
                    future.cancel()

        handlers = dict(kwargs.pop('verb_handlers', None) or {})
        check('status' not in handlers, 'status callback already owned')
        handlers['status'] = status
        original_server_init(self, *args, verb_handlers=handlers, **kwargs)

    def capture_profile_config(original):
        async def consume(self, profile_name, *args, **kwargs):
            token = consuming_profile.set((self, profile_name))
            try:
                return await original(self, profile_name, *args, **kwargs)
            finally:
                consuming_profile.reset(token)
        return consume

    # Capture only configuration consumed by real startup/reconnect, never
    # unrelated reads of a file that the running adapter has not adopted.
    for name in ('_start_one_profile_adapters', '_run_secondary_profile_reconnect'):
        original = getattr(gateway.GatewayRunner, name)
        setattr(gateway.GatewayRunner, name, capture_profile_config(original))
    config_module.load_gateway_config = load_config
    gateway.GatewayRunner._create_adapter = create_adapter
    gateway.GatewayRunner.__init__ = runner_init
    control_socket.GatewayControlServer.__init__ = server_init
