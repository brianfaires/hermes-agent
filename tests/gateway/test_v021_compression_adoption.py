"""Gateway adopts a complete published child without destructive followup writes."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from hermes_state import SessionDB
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource

class CompressionAdoptionTests(unittest.IsolatedAsyncioTestCase):
    async def check_adoption(self, save_fails=False):
        with tempfile.TemporaryDirectory() as tmp:
            db = SessionDB(db_path=Path(tmp)/'state.db')
            self.addCleanup(db.close)
            db.create_session('original', source='telegram')
            history = [{'role': role, 'content': 'historic '*100} for role in ['user','assistant']*5]
            for m in history: db.append_message('original', m['role'], m['content'])
            runner = object.__new__(GatewayRunner)
            entry = SimpleNamespace(session_id='original', session_key='caller')
            runner.session_store = object()
            runner._async_session_store = SimpleNamespace(_store=runner.session_store,
                get_or_create_session=AsyncMock(return_value=entry),
                load_transcript=AsyncMock(return_value=history),
                rewrite_transcript=AsyncMock(side_effect=AssertionError('must never rewrite published child')),
                update_session=AsyncMock(), _save=AsyncMock(side_effect=RuntimeError('route save failed') if save_fails else None))
            runner._session_db = SimpleNamespace(_db=db, get_session=AsyncMock(return_value=db.get_session('original')))
            runner._session_key_for_source = lambda source: 'caller'
            runner._resolve_session_agent_runtime = lambda *args, **kwargs: ('test', {'api_key':'fixture'})
            runner._evict_cached_agent = Mock()
            runner._sync_telegram_topic_binding = Mock()
            runner._cleanup_agent_resources_off_loop = AsyncMock()
            async def execute(fn): return fn()
            runner._run_in_executor_with_context = execute
            compressor = SimpleNamespace(has_content_to_compress=lambda head: True,
                _last_compress_aborted=False, _last_summary_error=None,
                _last_aux_model_failure_model=None, _last_aux_model_failure_error=None)
            agent = SimpleNamespace(session_id='original', tools=None, _cached_system_prompt='',
                _last_compaction_in_place=False, _compression_skipped_due_to_lock=False,
                context_compressor=compressor)
            def compress(messages, system, **kwargs):
                self.assertEqual(messages, history)
                self.assertEqual(kwargs['preserve_tail_count'], 2)
                compressed = [{'role':'assistant','content':'summary'}]+history[-2:]
                self.assertTrue(db.try_acquire_compression_lock('original', 'fixture'))
                db.publish_compression_child(parent_session_id='original', child_session_id='child',
                    source='telegram', model='test', messages=compressed, compression_lock_holder='fixture')
                db.release_compression_lock('original', 'fixture')
                db.append_message('child', 'user', 'concurrent child append')
                agent.session_id='child'
                return compressed, ''
            agent._compress_context = compress
            event = MessageEvent(text='/compress here 1', source=SessionSource(platform=Platform.TELEGRAM,chat_id='chat'))
            with patch('run_agent.AIAgent', return_value=agent), patch('agent.conversation_compression.finalize_context_engine_compression_notification') as finalize:
                result=await runner._handle_compress_command_inner(event)
            self.assertEqual(entry.session_id, 'child')
            runner._async_session_store.rewrite_transcript.assert_not_awaited()
            self.assertEqual(db.get_messages_as_conversation('child')[-1]['content'], 'concurrent child append')
            self.assertEqual(db.get_session('original')['end_reason'], 'compression')
            if save_fails:
                self.assertIn('failed', result.lower())
                runner._async_session_store.update_session.assert_not_awaited()
                self.assertFalse(any(c.kwargs.get('committed') for c in finalize.call_args_list))
            else:
                self.assertNotIn('failed', result.lower())
                runner._async_session_store.update_session.assert_awaited_once()

    async def test_published_child_is_adopted_without_rewrite(self):
        await self.check_adoption()

    async def test_routing_save_failure_never_deletes_canonical_child(self):
        await self.check_adoption(save_fails=True)
