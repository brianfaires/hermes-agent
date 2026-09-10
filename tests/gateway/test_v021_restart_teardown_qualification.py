"""Drive real request_restart -> stop; isolate all process/transport effects."""
import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch


class RestartTeardownQualification(unittest.IsolatedAsyncioTestCase):
    async def test_shared_service_restart_tears_down_adapters_without_self_cancel(self):
        import gateway.run as gw
        from gateway.config import Platform
        from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE

        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'HOME': tmp, 'HERMES_HOME': tmp,
                'HERMES_BUNDLES_DIR': tmp + '/bundles'}))
            stack.enter_context(patch.object(gw, '_hermes_home', Path(tmp)))
            # Only effects at the host/process boundary are stubbed. Scheduling,
            # drain, stop task cancellation, adapter traversal and exit are real.
            for target in (
                'gateway.run.arm_shutdown_watchdog',
                'tools.process_registry.process_registry.kill_all',
                'cron.scheduler.mark_running_jobs_interrupted',
                'tools.async_delegation.interrupt_all',
                'tools.terminal_tool.cleanup_all_environments',
                'tools.browser_tool.cleanup_all_browsers',
                'agent.auxiliary_client.shutdown_cached_clients',
                'gateway.status.remove_pid_file',
                'gateway.status.release_gateway_runtime_lock',
                'gateway.run._shutdown_gateway_health_export',
            ):
                stack.enter_context(patch(target, return_value=None))
            runner = object.__new__(gw.GatewayRunner)
            for name in ('_restart_task_started', '_restart_requested', '_draining',
                         '_external_drain_active', '_restart_detached', '_restart_via_service'):
                setattr(runner, name, False)
            for name in ('_stop_task', '_restart_task', '_exit_reason', '_exit_code',
                         '_systemd_watchdog', '_restart_command_source'):
                setattr(runner, name, None)
            for name in ('_running_agents', '_running_agents_ts', '_pending_messages',
                         '_pending_approvals', '_profile_failed_platforms'):
                setattr(runner, name, {})
            runner._running = True
            runner._restart_after_turn_timeout = 0
            runner._restart_drain_timeout = 0
            runner._shutdown_event = asyncio.Event()
            runner._active_cron_job_count = runner._active_api_run_count = lambda: 0
            runner._update_runtime_status = Mock()
            runner._clear_plugin_message_injector = Mock()
            runner._stop_loop_liveness_guards = Mock()
            runner._notify_active_sessions_of_shutdown = AsyncMock()
            runner._stop_hosted_room_worker = AsyncMock(return_value=True)
            primary = Mock(cancel_background_tasks=AsyncMock(), disconnect=AsyncMock())
            secondary = Mock(cancel_background_tasks=AsyncMock(), disconnect=AsyncMock())
            runner.adapters = {Platform.TELEGRAM: primary}
            runner._profile_adapters = {'secondary': {Platform.TELEGRAM: secondary}}
            decoy = asyncio.create_task(asyncio.Event().wait())
            runner._background_tasks = {decoy}
            try:
                self.assertTrue(runner.request_restart(detached=False, via_service=True))
                restart = runner._restart_task
                self.assertNotIn(restart, runner._background_tasks)
                await asyncio.wait_for(restart, 5)
                await asyncio.gather(decoy, return_exceptions=True)
                self.assertFalse(restart.cancelled())
                self.assertTrue(decoy.cancelled())
                self.assertTrue(runner._shutdown_event.is_set())
                self.assertEqual(runner._exit_code, GATEWAY_SERVICE_RESTART_EXIT_CODE)
                for adapter in (primary, secondary):
                    adapter.cancel_background_tasks.assert_awaited_once()
                    adapter.disconnect.assert_awaited_once()
                self.assertEqual(runner.adapters, {})
                self.assertEqual(runner._profile_adapters, {})
                self.assertTrue((Path(tmp) / '.clean_shutdown').exists())
            finally:
                pending = [t for t in (decoy, runner._restart_task, runner._stop_task) if t and not t.done()]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
