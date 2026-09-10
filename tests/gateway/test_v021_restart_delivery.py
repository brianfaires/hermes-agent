"""Restart must await actual adapter delivery after runner work is released."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_context import set_session_vars, clear_session_vars


class Adapter(BasePlatformAdapter):
    async def connect(self, **kwargs): return True
    async def disconnect(self): pass
    async def get_chat_info(self, chat_id): return {'type': 'dm'}
    async def send(self, chat_id, content, **kwargs):
        self.sending.set()
        await self.release.wait()
        return SendResult(success=True, message_id='sent')


class RestartDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_model_restart_waits_through_real_adapter_delivery(self):
        await self._delivery_case(hanging_callback=False)

    async def test_prior_callback_timeout_cannot_strand_restart(self):
        with patch('gateway.platforms.base._POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS', .05):
            await self._delivery_case(hanging_callback=True)

    async def _delivery_case(self, *, hanging_callback):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'HERMES_HOME': home}):
            Path(home, 'config.yaml').write_text('plugins:\n  enabled: [gateway-restart-tool]\n')
            path = Path(__file__).resolve().parents[2] / 'plugins/gateway-restart-tool/__init__.py'
            spec = importlib.util.spec_from_file_location('restart_regression', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            runner = object.__new__(GatewayRunner)
            runner._gateway_loop = asyncio.get_running_loop()
            runner._restart_task_started = runner._restart_requested = runner._draining = False
            runner._running_agents = {'caller': SimpleNamespace()}
            runner._active_cron_job_count = runner._active_api_run_count = lambda: 0
            runner._wedged_agent_count = lambda: 0
            runner._restart_after_turn_timeout = 0
            runner.stop = AsyncMock()
            adapter = Adapter(PlatformConfig(), Platform.TELEGRAM)
            adapter.config.typing_indicator = False
            adapter.sending, adapter.release = asyncio.Event(), asyncio.Event()
            runner.adapters = {}
            runner._profile_adapters = {'secondary': {Platform.TELEGRAM: adapter}}
            event = MessageEvent(text='restart', source=SessionSource(platform=Platform.TELEGRAM, chat_id='chat'))
            interrupt = asyncio.Event()
            interrupt._hermes_run_generation = 7
            adapter._active_sessions['caller'] = interrupt
            if hanging_callback:
                async def prior_callback():
                    await asyncio.Event().wait()
                adapter.register_post_delivery_callback('caller', prior_callback, generation=7)

            async def handler(event):
                tokens = set_session_vars(platform='telegram', session_key='caller')
                try:
                    result = json.loads(await asyncio.to_thread(module._handle_request_gateway_restart,
                        {'reason': 'test', 'confirm': 'restart gateway'}))
                    self.assertTrue(result['ok'], result)
                finally:
                    clear_session_vars(tokens)
                runner._running_agents.clear()
                return 'Restart scheduled.'

            adapter._message_handler = handler
            with patch('gateway.run._gateway_runner_ref', lambda: runner), patch.object(module, '_restart_modes', lambda: (False, True)):
                task = asyncio.create_task(adapter._process_message_background(event, 'caller'))
                try:
                    await asyncio.wait_for(adapter.sending.wait(), 3)
                    # Flush the restart coroutine past its normal 50ms stop delay.
                    await asyncio.sleep(.15)
                    runner.stop.assert_not_awaited()
                    self.assertIsNone(adapter.pop_post_delivery_callback('caller', generation=6))
                    adapter.release.set()
                    await asyncio.wait_for(task, 3)
                    await asyncio.wait_for(runner._restart_task, 3)
                    runner.stop.assert_awaited_once()
                    audit = module._audit_path().read_text()
                    self.assertIn('"scheduled"', audit)
                finally:
                    adapter.release.set()
                    await asyncio.gather(task, return_exceptions=True)
                    restart = getattr(runner, '_restart_task', None)
                    if restart and not restart.done():
                        restart.cancel()
                        await asyncio.gather(restart, return_exceptions=True)

    async def test_busy_slash_waits_for_its_own_inline_reply(self):
        from gateway.session import build_session_key
        with tempfile.TemporaryDirectory() as home, patch('gateway.run._hermes_home', Path(home)):
            runner = object.__new__(GatewayRunner)
            runner._restart_task_started = runner._restart_requested = runner._draining = False
            runner._await_active_work_before_restart = AsyncMock()
            runner.stop = AsyncMock()
            runner._is_stale_restart_redelivery = lambda event: False
            runner._running_agent_count = lambda: 1
            adapter = Adapter(PlatformConfig(), Platform.TELEGRAM)
            adapter._heal_stale_session_lock = lambda key: None
            adapter.sending, adapter.release = asyncio.Event(), asyncio.Event()
            runner.adapters = {Platform.TELEGRAM: adapter}
            event = MessageEvent(text='/restart', source=SessionSource(platform=Platform.TELEGRAM, chat_id='chat'))
            key = build_session_key(event.source, group_sessions_per_user=True,
                                    thread_sessions_per_user=False, profile=adapter._session_key_profile(event.source))
            runner._session_key_for_source = lambda source: key
            guard = asyncio.Event()
            guard._hermes_run_generation = 7
            adapter._active_sessions[key] = guard
            adapter._message_handler = runner._handle_restart_command
            with patch('gateway.restart.is_gateway_supervisor_process', return_value=True):
                task = asyncio.create_task(adapter.handle_message(event))
                try:
                    await asyncio.wait_for(adapter.sending.wait(), 3)
                    # The original turn finishes while the slash reply is still sending.
                    callback = adapter.pop_post_delivery_callback(key, generation=7)
                    callback()
                    await asyncio.sleep(.15)
                    self.assertFalse(task.done())
                    runner.stop.assert_not_awaited()
                    adapter.release.set()
                    await asyncio.wait_for(task, 3)
                    await asyncio.wait_for(runner._restart_task, 3)
                    runner.stop.assert_awaited_once()
                finally:
                    adapter.release.set()
                    await asyncio.gather(task, return_exceptions=True)
                    restart = getattr(runner, '_restart_task', None)
                    if restart and not restart.done():
                        restart.cancel()
                        await asyncio.gather(restart, return_exceptions=True)

    async def test_slash_restart_passes_caller_delivery_key(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as home, patch('gateway.run._hermes_home', Path(home)):
            runner = object.__new__(GatewayRunner)
            runner._restart_requested = runner._draining = False
            runner._is_stale_restart_redelivery = lambda event: False
            runner._running_agent_count = lambda: 0
            runner._session_key_for_source = lambda source: 'slash-caller'
            runner.request_restart = Mock(return_value=True)
            event = MessageEvent(text='/restart', source=SessionSource(platform=Platform.TELEGRAM, chat_id='chat'))
            with patch('gateway.restart.is_gateway_supervisor_process', return_value=True):
                await runner._handle_restart_command(event)
            runner.request_restart.assert_called_once_with(
                detached=False, via_service=True,
                defer_until_session_delivered='slash-caller', defer_until_delivery=None,
            )

    async def test_restart_audit_failure_still_denies_before_scheduling(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'HERMES_HOME': home}):
            Path(home, 'config.yaml').write_text('plugins:\n  enabled: [gateway-restart-tool]\n')
            path = Path(__file__).resolve().parents[2] / 'plugins/gateway-restart-tool/__init__.py'
            spec = importlib.util.spec_from_file_location('restart_denial_regression', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            runner = SimpleNamespace(_running_agents={'caller': object()},
                _active_work_count=lambda: 1, _running_agent_count=lambda: 1,
                _restart_requested=False, _draining=False)
            tokens = set_session_vars(platform='telegram', session_key='caller')
            try:
                with patch.object(module, '_resolve_runner', lambda: runner), \
                     patch.object(module, '_append_audit', side_effect=OSError('unavailable')), \
                     patch.object(module, '_schedule_restart') as schedule:
                    result = json.loads(module._handle_request_gateway_restart(
                        {'reason': 'test', 'confirm': 'restart gateway'}))
                    self.assertFalse(result['ok'])
                    self.assertEqual(result['error'], 'audit_unavailable')
                    schedule.assert_not_called()
            finally:
                clear_session_vars(tokens)


if __name__ == '__main__': unittest.main()
