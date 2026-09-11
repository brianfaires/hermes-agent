"""Execute manual compression failure bookkeeping without a provider call."""
import asyncio
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class CompressionFailure(unittest.IsolatedAsyncioTestCase):
    async def test_no_rotation_is_failure_without_success_bookkeeping(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'HERMES_HOME': home}):
            runner = object.__new__(GatewayRunner)
            entry = SimpleNamespace(session_id='original', session_key='caller')
            history = [{'role': role, 'content': 'keep original'} for role in ['user','assistant'] * 3]
            runner.session_store = object()
            runner._async_session_store = SimpleNamespace(_store=runner.session_store,
                get_or_create_session=AsyncMock(return_value=entry),
                load_transcript=AsyncMock(return_value=history),
                rewrite_transcript=AsyncMock(), update_session=AsyncMock())
            runner._session_db = None
            runner._session_key_for_source = lambda source: 'caller'
            runner._resolve_session_agent_runtime = lambda *args, **kwargs: ('test', {'api_key': 'test'})
            runner._evict_cached_agent = Mock()
            runner._cleanup_agent_resources_off_loop = AsyncMock()
            async def execute(fn): return fn()
            runner._run_in_executor_with_context = execute
            compressor = SimpleNamespace(has_content_to_compress=lambda head: True,
                _last_compress_aborted=False, _last_summary_error=None,
                _last_aux_model_failure_model=None, _last_aux_model_failure_error=None)
            agent = SimpleNamespace(session_id='original', tools=None,
                _last_compaction_in_place=False, _compression_skipped_due_to_lock=False,
                context_compressor=compressor,
                _compress_context=lambda *args, **kwargs: ([{'role':'assistant','content':'summary'}], ''))
            event = MessageEvent(text='/compress', source=SessionSource(platform=Platform.TELEGRAM,chat_id='chat'))
            with patch('run_agent.AIAgent', return_value=agent), \
                 patch('agent.conversation_compression.finalize_context_engine_compression_notification') as finalize:
                result = await runner._handle_compress_command_inner(event)
            self.assertIn('failed', result.lower())
            self.assertEqual(entry.session_id, 'original')
            runner._async_session_store.rewrite_transcript.assert_not_awaited()
            runner._async_session_store.update_session.assert_not_awaited()
            self.assertFalse(any(call.kwargs.get('committed') for call in finalize.call_args_list))
            runner._cleanup_agent_resources_off_loop.assert_awaited_once()


if __name__ == '__main__': unittest.main()
