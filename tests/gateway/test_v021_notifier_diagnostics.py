"""Outer notifier failures retain debugging context without sending a message."""
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway import kanban_watchers
from gateway.config import Platform
from hermes_cli import kanban_db


class NotifierDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_collection_failure_logs_profile_board_and_traceback(self):
        with tempfile.TemporaryDirectory() as home:
            runner = SimpleNamespace(_running=True, adapters={Platform.TELEGRAM: object()}, _profile_adapters={}, _kanban_notifier_profile='test-profile', _owns_kanban_dispatcher_lock=lambda: False)
            def fail(*args, **kwargs):
                runner._running = False
                raise RuntimeError('collection failed')
            connection = MagicMock()
            with patch.dict('os.environ', {'HERMES_HOME': home}), patch.object(kanban_watchers.asyncio, 'sleep', AsyncMock()), patch.object(kanban_db, 'list_boards', return_value=[{'slug': 'test-board', 'db_path': home + '/board.db'}]), patch.object(kanban_db, 'count_notify_subs', return_value=1), patch.object(kanban_db, 'connect', return_value=connection), patch.object(kanban_db, 'purge_stale_done_notify_subs', return_value=0), patch.object(kanban_db, 'list_notify_subs', side_effect=fail), self.assertLogs(kanban_watchers.logger, level='WARNING') as captured:
                await kanban_watchers.GatewayKanbanWatchersMixin._kanban_notifier_watcher(runner)
            records = [r for r in captured.records if 'notifier tick failed' in r.getMessage()]
            self.assertEqual(len(records), 1)
            self.assertIsNotNone(records[0].exc_info)
            self.assertIn('test-profile', records[0].getMessage())
            self.assertIn('test-board', records[0].getMessage())
            self.assertIn('collect_subscriptions', records[0].getMessage())
            connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
