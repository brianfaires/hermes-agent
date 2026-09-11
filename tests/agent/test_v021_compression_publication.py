"""Manual partial transcripts publish once, under the existing core transaction."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from hermes_state import SessionDB
from agent.conversation_compression import CompressionCommitFence

class CompressionPublicationTests(unittest.TestCase):
    def agent(self, db, in_place=False):
        from run_agent import AIAgent
        agent = AIAgent(api_key='fixture', base_url='https://example.invalid/v1',
            model='test/model', platform='telegram', quiet_mode=True,
            session_db=db, session_id='parent', skip_memory=True, skip_context_files=True)
        self.addCleanup(agent.close)
        agent._end_session_on_close = False
        compressor = MagicMock()
        compressor.compression_count = 1
        compressor.last_prompt_tokens = compressor.last_completion_tokens = 0
        for name in ('_last_summary_error', '_last_aux_model_failure_model', '_last_aux_model_failure_error'):
            setattr(compressor, name, None)
        compressor._last_compress_aborted = compressor._last_summary_auth_failure = False
        compressor.compress.return_value = [
            {'role': 'user', 'content': '[CONTEXT COMPACTION] summary'},
            {'role': 'assistant', 'content': 'Ready.'}]
        agent.context_compressor = compressor
        agent.compression_in_place = in_place
        return agent

    def fixture(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        db = SessionDB(db_path=Path(temp.name) / 'state.db'); self.addCleanup(db.close)
        db.create_session('parent', source='telegram', profile_name='fixture')
        messages = [{'role': role, 'content': 'historical '+str(i)+' '+('long text '*100)}
                    for i, role in enumerate(['user', 'assistant']*8)]
        messages += [{'role': 'user', 'content': 'verbatim tail question'},
                     {'role': 'assistant', 'content': 'verbatim tail answer'}]
        for m in messages: db.append_message('parent', m['role'], m['content'])
        return db, db.get_messages_as_conversation('parent')

    def test_partial_tail_is_in_atomic_child_and_foreign_append_survives(self):
        db, messages = self.fixture(); agent = self.agent(db)
        original = copy.deepcopy(messages)
        publish = db.publish_compression_child
        def publish_with_new_child_tail(**kwargs):
            result = publish(**kwargs)
            db.append_message(kwargs['child_session_id'], 'user', 'concurrent child message')
            return result
        with patch.object(db, 'publish_compression_child', side_effect=publish_with_new_child_tail):
            agent._compress_context(messages, '', force=True, preserve_tail_count=2,
                                    commit_fence=CompressionCommitFence())
        self.assertNotEqual(agent.session_id, 'parent')
        supplied = agent.context_compressor.compress.call_args.args[0]
        self.assertEqual(supplied, original[:-2])
        rows = db.get_messages_as_conversation(agent.session_id)
        contents = [m.get('content') for m in rows]
        for text in ('verbatim tail question', 'verbatim tail answer', 'concurrent child message'):
            self.assertEqual(contents.count(text), 1)
        self.assertEqual(db.get_session(agent.session_id)['profile_name'], 'fixture')

    def test_publication_failure_preserves_parent_and_input(self):
        db, messages = self.fixture(); agent = self.agent(db)
        before = copy.deepcopy(messages)
        with patch.object(db, 'publish_compression_child', side_effect=RuntimeError('fixture commit failure')):
            agent._compress_context(messages, '', force=True, preserve_tail_count=2,
                                    commit_fence=CompressionCommitFence())
        self.assertEqual(agent.session_id, 'parent')
        self.assertEqual(messages, before)
        self.assertIsNone(db.get_session('parent')['ended_at'])
        self.assertEqual(db.get_messages_as_conversation('parent'), before)

    def test_in_place_preserves_tail_in_same_commit(self):
        db, messages = self.fixture(); agent = self.agent(db, in_place=True)
        agent._compress_context(messages, '', force=True, preserve_tail_count=2,
                                commit_fence=CompressionCommitFence())
        self.assertEqual(agent.session_id, 'parent')
        self.assertTrue(agent._last_compaction_in_place)
        contents = [m.get('content') for m in db.get_messages_as_conversation('parent')]
        self.assertEqual(contents.count('verbatim tail question'), 1)
        self.assertEqual(contents.count('verbatim tail answer'), 1)
