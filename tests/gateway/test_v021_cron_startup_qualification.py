"""Real gateway startup passes allowlisted profile homes to the real ticker.

Service/network setup is replaced at its effects; no gateway is activated.
"""
import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


class CronStartupQualification(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_dispatches_selected_homes_and_owned_adapters(self):
        import gateway.run as gw
        from gateway.config import GatewayConfig
        from cron import jobs
        from cron.scheduler_provider import InProcessCronScheduler

        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            home = Path(tmp) / '.hermes'
            home.mkdir()
            for name in ('selected', 'excluded'):
                (home / 'profiles' / name).mkdir(parents=True)
            stack.enter_context(patch.dict(os.environ, {'HOME': tmp, 'HERMES_HOME': str(home),
                'HERMES_BUNDLES_DIR': tmp + '/bundles'}))
            stack.enter_context(patch.object(Path, 'home', return_value=Path(tmp)))
            stack.enter_context(patch.object(gw, '_hermes_home', home))
            config = GatewayConfig(multiplex_profiles=True, multiplex_profile_allowlist=['selected'])
            shared, secondary = {'fixture': 'default'}, {'fixture': 'selected'}
            runner = SimpleNamespace(config=config, adapters=shared,
                _profile_adapters={'selected': secondary}, _draining=False, _external_drain_active=False,
                _running=True, _restart_requested=False, _restart_via_service=False,
                start=AsyncMock(return_value=True), wait_for_shutdown=AsyncMock(),
                should_exit_cleanly=False, should_exit_with_failure=False, exit_code=None)
            stack.enter_context(patch.object(gw, 'GatewayRunner', return_value=runner))
            for target in (
                'hermes_cli.resource_limits.apply_nofile_soft_limit',
                'gateway.code_skew.record_boot_fingerprint', 'tools.skills_sync.sync_skills',
                'hermes_logging.setup_logging', 'hermes_cli.security_audit_startup.log_startup_security_warnings',
                'gateway.status.write_pid_file', 'gateway.status.remove_pid_file',
                'gateway.status.release_gateway_runtime_lock', 'atexit.register',
                'gateway.lifecycle_ledger.record_startup',
                'hermes_cli.nous_auth_keepalive.start_nous_auth_keepalive',
                'hermes_cli.nous_auth_keepalive.stop_nous_auth_keepalive',
                'tools.mcp_tool.discover_mcp_tools', 'gateway.shutdown_flush.recover_pending_to_db',
            ):
                stack.enter_context(patch(target, return_value=None))
            stack.enter_context(patch('gateway.status.get_running_pid', return_value=None))
            stack.enter_context(patch('gateway.status.acquire_gateway_runtime_lock', return_value=True))
            stack.enter_context(patch('gateway.control_socket.GatewayControlServer', side_effect=RuntimeError('fixture: no socket')))
            stack.enter_context(patch.object(gw, '_shutdown_mcp_servers_nonblocking', AsyncMock()))
            loop = asyncio.get_running_loop()
            stack.enter_context(patch.object(loop, 'add_signal_handler'))
            stack.enter_context(patch.object(loop, 'set_exception_handler'))
            captured = []
            real_thread = threading.Thread
            def thread_factory(*args, **kwargs):
                if kwargs.get('name') in {'cron-scheduler', 'gateway-housekeeping', 'planned-stop-watcher'}:
                    captured.append(kwargs)
                    return Mock(is_alive=Mock(return_value=False))
                return real_thread(*args, **kwargs)
            stack.enter_context(patch.object(threading, 'Thread', side_effect=thread_factory))
            self.assertTrue(await gw.start_gateway(config, verbosity=None))
            cron = next(entry for entry in captured if entry['name'] == 'cron-scheduler')
            self.assertIsInstance(cron['target'].__self__, InProcessCronScheduler)
            self.assertEqual(cron['kwargs']['profile_homes'], [
                ('default', home), ('selected', home / 'profiles/selected')])
            # Invoke the actual captured provider with those exact kwargs. Only
            # job execution is replaced: observe its scoped store and adapter.
            stop = threading.Event()
            seen = []
            def tick(**kwargs):
                seen.append((jobs._current_cron_store().cron_dir.parent, kwargs['adapters']))
                if len(seen) == 2:
                    stop.set()
                return 0
            with patch('cron.scheduler.tick', side_effect=tick):
                cron['target'](stop, **cron['kwargs'], interval=0)
            self.assertEqual(seen, [(home, shared), (home / 'profiles/selected', secondary)])
            for profile in (home, home / 'profiles/selected'):
                self.assertTrue((profile / 'cron/ticker_heartbeat').exists())
            self.assertFalse((home / 'profiles/excluded/cron').exists())


if __name__ == '__main__':
    unittest.main()
