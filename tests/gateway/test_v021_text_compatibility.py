"""Approved literal rendering, standalone MEDIA and free-response routing."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from gateway.config import PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, _strip_media_tag_directives
from plugins.platforms.discord import adapter as mod

class TextCompatibility(unittest.TestCase):
    def test_literal_prose_preserves_code_and_converts_tables(self):
        adapter = object.__new__(mod.DiscordAdapter)
        self.assertEqual(adapter.format_message('a_b *c* `a_b`\n```py\na_b *c*\n```'), 'a\\_b \\*c\\* `a_b`\n```py\na_b *c*\n```')
        result = adapter.format_message('| Key | Value |\n| --- | --- |\n| a_b | c |')
        self.assertNotIn('| --- |', result)
        self.assertIn('a\\_b', result)

    def test_standalone_media_extract_and_cleanup_agree(self):
        for directive in ['MEDIA:/tmp/a.pdf', '  MEDIA:"/tmp/a b.pdf"', '- **MEDIA:/tmp/a.pdf**']:
            with self.subTest(directive=directive):
                media, cleaned = BasePlatformAdapter.extract_media(directive)
                self.assertEqual(len(media), 1)
                self.assertEqual(cleaned.strip(), '')
                self.assertEqual(_strip_media_tag_directives(directive).strip(), '')

    def test_prose_code_json_and_adjacent_tags_stay_literal(self):
        for text in ['See MEDIA:/tmp/a.pdf now', '`code` MEDIA:/tmp/a.pdf', 'MEDIA:/tmp/a.pdf is an example', '`example MEDIA:/tmp/a.pdf`', '```\nMEDIA:/tmp/a.pdf\n```', '> MEDIA:/tmp/a.pdf', '{"x":"MEDIA:/tmp/a.pdf"}', 'MEDIA:/tmp/a.pdfMEDIA:/tmp/b.pdf']:
            with self.subTest(text=text):
                media, cleaned = BasePlatformAdapter.extract_media(text)
                self.assertEqual(media, [])
                self.assertEqual(cleaned, text)
                self.assertEqual(_strip_media_tag_directives(text), text)

    def test_unknown_extension_real_file_validation_and_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'space name.custom'; path.write_text('public artifact')
            text=f'MEDIA:{path}'
            media, cleaned=BasePlatformAdapter.extract_media(text)
            self.assertEqual(media, [(str(path), False)])
            self.assertEqual(cleaned, '')
            path.unlink()
            self.assertEqual(BasePlatformAdapter.extract_media(text), ([], text))

class Channel:
    id=789; name='general'; topic=None
    guild=SimpleNamespace(name='fixture')
    def __init__(self): self.send=AsyncMock()
    def history(self, **kwargs):
        async def items():
            if False: yield
        return items()

class Thread(Channel):
    id=456; name='task'
    def __init__(self, parent):
        super().__init__(); self.parent=parent; self.parent_id=parent.id

class FreeResponseRouting(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, no_thread=False, failed=False, enabled=True):
        with patch.dict(os.environ, {'DISCORD_REQUIRE_MENTION':'true','DISCORD_FREE_RESPONSE_CHANNELS':'789','DISCORD_AUTO_THREAD':str(enabled).lower(),'DISCORD_NO_THREAD_CHANNELS':'789' if no_thread else ''}), patch.object(mod.discord,'Thread',Thread):
            adapter=mod.DiscordAdapter(PlatformConfig(enabled=True, token='fixture'))
            adapter._client=SimpleNamespace(user=SimpleNamespace(id=999))
            adapter._text_batch_delay_seconds=0
            adapter.handle_message=AsyncMock()
            channel=Channel(); thread=Thread(channel)
            adapter._auto_create_thread=AsyncMock(return_value=None if failed else thread)
            msg=SimpleNamespace(id=123, content='ordinary free-response text', mentions=[], attachments=[], reference=None, created_at=datetime.now(timezone.utc), channel=channel, author=SimpleNamespace(id=42, display_name='tester',name='tester'), type=mod.discord.MessageType.default)
            await adapter._handle_message(msg)
            return adapter, channel

    async def test_free_response_uses_configured_thread_and_route(self):
        adapter, _=await self.exercise()
        adapter._auto_create_thread.assert_awaited_once()
        adapter.handle_message.assert_awaited_once()
        source=adapter.handle_message.await_args.args[0].source
        self.assertEqual(source.thread_id, '456')
        self.assertEqual(source.chat_id, '456')

    async def test_failed_thread_does_not_invoke_agent_inline(self):
        adapter, channel=await self.exercise(failed=True)
        adapter.handle_message.assert_not_awaited()
        channel.send.assert_awaited_once()

    async def test_explicit_no_thread_and_disabled_auto_thread_stay_inline(self):
        for options in ({'no_thread':True}, {'enabled':False}):
            adapter, _=await self.exercise(**options)
            adapter._auto_create_thread.assert_not_awaited()
            self.assertEqual(adapter.handle_message.await_args.args[0].source.chat_id, '789')
