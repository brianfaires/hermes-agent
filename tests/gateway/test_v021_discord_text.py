import asyncio
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


class DiscordTextTests(unittest.TestCase):
    def adapter(self):
        return DiscordAdapter(PlatformConfig())

    def test_successful_fingerprint_skips_but_failed_attempt_retries(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'HERMES_HOME': tmp}):
            adapter = self.adapter()
            adapter._record_command_sync_success('1', 'old', {})
            self.assertIsNotNone(adapter._command_sync_skip_reason('1', 'old'))
            adapter._record_command_sync_attempt('1', 'new')
            self.assertIsNone(adapter._command_sync_skip_reason('1', 'new'))
            adapter._record_command_sync_rate_limit('1', 'new', 10)
            self.assertIn('wait', adapter._command_sync_skip_reason('1', 'new'))
            self.assertTrue(adapter._command_sync_state_path().is_file())

    def test_progress_send_edit_suppress_embeds_without_changing_normal_send(self):
        async def run():
            adapter = self.adapter()
            message = types.SimpleNamespace(id=3, edit=AsyncMock())
            channel = types.SimpleNamespace(id=1, name='test', parent_id=None, parent=None, type=0, send=AsyncMock(return_value=message), get_partial_message=lambda ident: message)
            adapter._client = types.SimpleNamespace(get_channel=lambda ident: channel)
            result = await adapter.send('1', 'https://example.com', metadata={'suppress_embeds': True})
            self.assertTrue(result.success, result.error)
            self.assertTrue(channel.send.call_args.kwargs.get('suppress_embeds'))
            result = await adapter.edit_message('1', '3', 'progress', metadata={'suppress_embeds': True})
            self.assertTrue(result.success, result.error)
            self.assertTrue(message.edit.call_args.kwargs.get('suppress'))
            result = await adapter.edit_message('1', '3', 'a' * 2500, finalize=True, metadata={'suppress_embeds': True})
            self.assertTrue(result.success, result.error)
            self.assertTrue(message.edit.call_args.kwargs.get('suppress'))
            self.assertTrue(channel.send.call_args.kwargs.get('suppress_embeds'))
            await adapter.send('1', 'normal')
            self.assertNotIn('suppress_embeds', channel.send.call_args.kwargs)
        asyncio.run(run())

    def test_forum_progress_suppresses_starter_and_continuations(self):
        async def run():
            adapter = self.adapter()
            thread = types.SimpleNamespace(id=2, send=AsyncMock(return_value=types.SimpleNamespace(id=3)))
            forum = types.SimpleNamespace(id=1, create_thread=AsyncMock(return_value=types.SimpleNamespace(thread=thread, message=types.SimpleNamespace(id=2))))
            result = await adapter._send_to_forum(forum, 'a' * 2500, suppress_embeds=True)
            self.assertTrue(result.success, result.error)
            self.assertTrue(forum.create_thread.call_args.kwargs.get('suppress_embeds'))
            self.assertTrue(thread.send.call_args.kwargs.get('suppress_embeds'))
        asyncio.run(run())

    def test_current_table_and_intentional_markdown_preserved(self):
        adapter = self.adapter()
        self.assertEqual(adapter.format_message('**bold** and ~~removed~~'), '**bold** and ~~removed~~')
        result = adapter.format_message('| Name | Score |\n| --- | --- |\n| Alice | 95 |')
        self.assertIn('**Alice**', result)
        self.assertIn('• Score: 95', result)
