"""Runtime qualification of background completion notifications remaining opt-in."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway import run
from tools import terminal_tool
from tools.process_registry import ProcessSession, process_registry


class BackgroundNotifyRetainedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        for mocker in [patch.dict(os.environ, {'HERMES_HOME': str(self.home), 'HERMES_BACKGROUND_NOTIFICATIONS': ''}), patch.object(run, '_hermes_home', self.home)]:
            mocker.start()
            self.addCleanup(mocker.stop)

    def test_real_config_loader_defaults_off_and_validates_opt_in(self):
        path = self.home / 'config.yaml'
        for source, expected in [('', 'off'), ('display:\n  background_process_notifications: false\n', 'off'), ('display:\n  background_process_notifications: concise\n', 'concise'), ('display:\n  background_process_notifications: invalid\n', 'off')]:
            with self.subTest(source=source):
                path.write_text(source)
                self.assertEqual(run.GatewayRunner._load_background_notifications_mode(), expected)

    def test_real_handler_preserves_notify_and_legacy_mapping_without_spawn(self):
        for extra, complete, patterns in [({}, False, None), ({'notify': True}, True, None), ({'notify_on_complete': True}, True, None), ({'notify': False, 'notify_on_complete': True}, False, None), ({'notify': ['ready']}, False, ['ready']), ({'watch_patterns': ['ready']}, False, ['ready'])]:
            with self.subTest(extra=extra), patch.object(terminal_tool, 'terminal_tool', return_value='{}') as execute:
                terminal_tool._handle_terminal({'command': 'inert', 'background': True, **extra})
                self.assertEqual(execute.call_args.kwargs['notify_on_complete'], complete)
                self.assertEqual(execute.call_args.kwargs['watch_patterns'], patterns)

    async def test_real_watcher_silent_by_default_explicit_completion_enqueues(self):
        session = ProcessSession(id='inert-process', command='inert', exited=True, exit_code=0, output_buffer='finished')
        enqueue = AsyncMock(return_value=True)
        runner = SimpleNamespace(_load_background_notifications_mode=run.GatewayRunner._load_background_notifications_mode, _enqueue_process_completion_notification=enqueue)
        for explicit in [False, True]:
            with self.subTest(explicit=explicit), patch.object(process_registry, 'get', return_value=session), patch.object(process_registry, 'is_completion_consumed', return_value=False), patch.object(run.asyncio, 'sleep', AsyncMock()):
                enqueue.reset_mock()
                await run.GatewayRunner._run_process_watcher(runner, {'session_id': session.id, 'check_interval': 1, 'notify_on_complete': explicit})
                self.assertEqual(enqueue.await_count, int(explicit))


if __name__ == '__main__':
    unittest.main()
