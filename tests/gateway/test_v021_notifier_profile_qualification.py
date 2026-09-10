"""Qualify secondary adapter collection/delivery using a disposable board."""
import asyncio
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(success=True)


class NotifierProfileQualification(unittest.IsolatedAsyncioTestCase):
    async def test_secondary_only_platform_delivers_through_secondary_adapter(self):
        from gateway.config import Platform
        from gateway.run import GatewayRunner
        from hermes_cli import kanban_db as kb

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'HOME': tmp, 'HERMES_HOME': tmp + '/home',
            'HERMES_BUNDLES_DIR': tmp + '/bundles', 'HERMES_KANBAN_DB': tmp + '/board.db',
        }):
            with patch.object(Path, 'home', return_value=Path(tmp)):
                kb.init_db()
                conn = kb.connect()
                try:
                    tid = kb.create_task(conn, title='fixture notification', assignee='worker')
                    kb.add_notify_sub(conn, task_id=tid, platform='telegram', chat_id='fixture-chat',
                                      notifier_profile='secondary', delivery_mode='notify')
                    kb.complete_task(conn, tid, summary='fixture complete')
                finally:
                    conn.close()
                adapter = RecordingAdapter()
                runner = object.__new__(GatewayRunner)
                runner._running = True
                runner.adapters = {}
                runner._profile_adapters = {'secondary': {Platform.TELEGRAM: adapter}}
                runner._kanban_sub_fail_counts = {}
                runner._kanban_dispatcher_lock_handle = object()
                runner._active_profile_name = lambda: 'default'
                real_sleep = asyncio.sleep
                async def one_tick(delay):
                    if delay != 5:
                        runner._running = False
                    await real_sleep(0)
                with patch('gateway.kanban_watchers.asyncio.sleep', side_effect=one_tick):
                    await runner._kanban_notifier_watcher(interval=1)
                self.assertEqual(len(adapter.sent), 1)
                self.assertEqual(adapter.sent[0][0], 'fixture-chat')
                self.assertIn('fixture complete', adapter.sent[0][1])
                self.assertFalse(runner.adapters)


if __name__ == '__main__':
    unittest.main()
