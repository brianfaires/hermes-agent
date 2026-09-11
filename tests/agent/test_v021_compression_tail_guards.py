"""Explicit partial boundaries survive adoption and post-summary transforms."""
import copy
import unittest
from unittest.mock import patch
import test_v021_compression_publication as fixtures
from agent.conversation_compression import CompressionCommitFence

class PreservedTailGuards(unittest.TestCase):
    agent = fixtures.CompressionPublicationTests.agent
    fixture = fixtures.CompressionPublicationTests.fixture

    def test_durable_parent_growth_keeps_original_requested_tail_out_of_summary(self):
        db, messages = self.fixture(); agent = self.agent(db)
        original = copy.deepcopy(messages)
        db.append_message('parent', 'user', 'new durable parent append')
        agent._compress_context(messages, '', force=True, preserve_tail_count=2,
                                commit_fence=CompressionCommitFence())
        self.assertNotEqual(agent.session_id, 'parent')
        self.assertEqual(agent.context_compressor.compress.call_args.args[0], original[:-2])
        contents = [m.get('content') for m in db.get_messages_as_conversation(agent.session_id)]
        for text in ('verbatim tail question', 'verbatim tail answer', 'new durable parent append'):
            self.assertEqual(contents.count(text), 1)

    def test_growing_candidate_refuses_without_salvaging_preserved_tail(self):
        db, messages = self.fixture(); agent = self.agent(db)
        original = copy.deepcopy(messages)
        agent.context_compressor.compress.return_value = [
            {'role':'user','content':'[CONTEXT COMPACTION] '+('oversized '*50000)},
            {'role':'assistant','content':'Ready.'}]
        with patch('agent.context_compressor.salvage_grown_transcript') as salvage:
            agent._compress_context(messages, '', force=True, preserve_tail_count=2,
                                    commit_fence=CompressionCommitFence())
        salvage.assert_not_called()
        self.assertEqual(agent.session_id, 'parent')
        self.assertEqual(db.get_messages_as_conversation('parent'), original)
        self.assertIsNone(db.get_session('parent')['ended_at'])

    def test_todo_refresh_does_not_change_preserved_last_user_message(self):
        db, messages = self.fixture(); agent = self.agent(db)
        db.append_message('parent', 'user', 'verbatim last user question')
        messages = db.get_messages_as_conversation('parent')
        agent._todo_store.format_for_injection = lambda: 'TODO fixture injection'
        agent._compress_context(messages, '', force=True, preserve_tail_count=3,
                                commit_fence=CompressionCommitFence())
        self.assertNotEqual(agent.session_id, 'parent')
        rows = db.get_messages_as_conversation(agent.session_id)
        self.assertEqual(rows[-1]['content'], 'verbatim last user question')
        self.assertEqual(sum('TODO fixture injection' in str(m.get('content')) for m in rows), 1)
