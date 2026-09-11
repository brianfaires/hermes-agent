"""Disposable-only credential-free adapter; never installed for deployment."""
import asyncio
from pathlib import Path
import socket
import time


def install():
    import release_dependency
    assert release_dependency.value == "reviewed"
    import gateway.run as gateway
    import gateway.config as config
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from hermes_constants import get_hermes_home
    import tools.tirith_security
    import tools.mcp_tool

    # Synchronize only deliberate marker faults with the real profile producer.
    # Normal ticker calls still execute the original producer unchanged.
    import fcntl
    import cron.jobs as cron_jobs
    original_record = cron_jobs.record_ticker_heartbeat
    def record_ticker_heartbeat(success=False):
        cron_dir = cron_jobs._current_cron_store().cron_dir
        cron_dir.mkdir(parents=True, exist_ok=True)
        with (cron_dir / '.fixture-marker-write.lock').open('a') as marker_lock:
            fcntl.flock(marker_lock, fcntl.LOCK_SH)
            try:
                original_record(success=success)
            finally:
                fcntl.flock(marker_lock, fcntl.LOCK_UN)
    cron_jobs.record_ticker_heartbeat = record_ticker_heartbeat

    # External downloads/discovery/warmup are not the subject of this test.
    tools.tirith_security.ensure_installed = lambda **kw: None
    tools.mcp_tool.discover_mcp_tools = lambda: None
    gateway.GatewayRunner._start_startup_warmup = lambda self: None
    gateway._start_gateway_housekeeping = lambda stop, **kw: stop.wait()
    # Fail if any other path tries credential/provider network activity.
    original_connect = socket.socket.connect
    def local_connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ('127.0.0.1', '::1'):
            raise RuntimeError('nonlocal network forbidden in preparation test')
        return original_connect(self, address)
    socket.socket.connect = local_connect

    def cfg():
        home = get_hermes_home()
        platforms = {config.Platform.DISCORD: config.PlatformConfig(enabled=True, token=home.name)}
        if home.parent.name != 'profiles':
            platforms[config.Platform.API_SERVER] = config.PlatformConfig(enabled=True, api_key='disposable')
        return config.GatewayConfig(
            sessions_dir=home / 'sessions', loop_watchdog=False, multiplex_profiles=True,
            platforms=platforms)
    config.load_gateway_config = cfg
    gateway.load_gateway_config_for_runner = cfg

    class LocalAdapter(BasePlatformAdapter):
        def __init__(self, platform, conf):
            super().__init__(conf, platform)
            self.home = get_hermes_home()
            self._client = None

        async def connect(self, **kwargs):
            self.listener = await asyncio.start_server(lambda r, w: w.close(), '127.0.0.1', 0)
            self._client = self.listener
            self._mark_connected()
            return True

        def _read_websocket_health(self, client):
            # Local stub transport only. Production Discord uses its own
            # ready/open/ACK predicate; no health records are stamped here.
            return (client.is_serving(), 'local-listener')

        async def disconnect(self):
            self.listener.close()
            await self.listener.wait_closed()
            self._mark_disconnected()

        async def get_chat_info(self, chat_id):
            return {'id': chat_id, 'type': 'private'}

        async def send(self, *args, **kwargs):
            return SendResult(success=True, message_id='local')

    from gateway.platforms.api_server import APIServerAdapter
    class LocalAPI(APIServerAdapter):
        async def connect(self, **kwargs):
            from aiohttp import web
            self._app = web.Application()
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, '127.0.0.1', 0)
            await self._site.start()
            self._mark_connected()
            return True

    gateway.GatewayRunner._create_adapter = lambda self, p, conf: (LocalAPI(conf) if p == config.Platform.API_SERVER else LocalAdapter(p, conf))
    original_start = gateway.GatewayRunner.start
    async def start(self):
        result = await original_start(self)
        # Fixture input drives actual work registries and adapter transport.
        # It does not replace observation or lifecycle producers.
        async def inputs():
            from cron.scheduler import try_register_running_job, release_running_job
            registered = False
            task = None
            api_task = None
            while self._running:
                secondary = get_hermes_home() / 'profiles' / 'secondary'
                api = self.adapters[config.Platform.API_SERVER]
                if (secondary / 'api-busy').exists() and api_task is None:
                    api_task = asyncio.create_task(asyncio.sleep(120))
                    api._active_run_tasks['disposable-secondary'] = api_task
                if not (secondary / 'api-busy').exists() and api_task:
                    api_task.cancel()
                    api._active_run_tasks.pop('disposable-secondary')
                    api_task = None
                want = (secondary / 'cron-busy').exists()
                if want and not registered:
                    registered = try_register_running_job('secondary:disposable')
                if not want and registered:
                    release_running_job('secondary:disposable')
                    registered = False
                if (secondary / 'background-busy').exists() and task is None:
                    task = asyncio.create_task(asyncio.sleep(120))
                    self._background_tasks.add(task)
                if not (secondary / 'background-busy').exists() and task:
                    task.cancel()
                    self._background_tasks.discard(task)
                    task = None
                if (secondary / 'disconnect').exists():
                    self._profile_adapters['secondary'][config.Platform.DISCORD].listener.close()
                await asyncio.sleep(.05)
        watcher = asyncio.create_task(inputs())
        watcher._hermes_supervised_watcher = True
        self._background_tasks.add(watcher)
        return result
    gateway.GatewayRunner.start = start
