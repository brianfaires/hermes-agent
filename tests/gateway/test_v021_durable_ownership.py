"""Real durable recovery/resume ownership and rollback, with no gateway effects."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionStore, SessionSource, _now
from hermes_state import SessionDB


class DurableOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = SessionDB(db_path=root / 'state.db')
        self.addCleanup(self.db.close)
        self.store = SessionStore(root / 'sessions', GatewayConfig())
        self.store._db = self.db
        identity = patch.object(self.store, '_active_profile_name', return_value='default')
        identity.start()
        self.addCleanup(identity.stop)
        self.source = SessionSource(platform=Platform.TELEGRAM, chat_id='destination',
                                    chat_type='dm', user_id='user')
        self.current = self.store.get_or_create_session(self.source)
        self.key = self.current.session_key

    def seed(self, sid='target', owner='default', key=None, parent=None):
        self.db.create_session(sid, 'telegram', session_key=key or self.key,
                               chat_id='destination', chat_type='dm', user_id='user',
                               parent_session_id=parent)
        self.db._execute_write(lambda conn: conn.execute(
            'UPDATE sessions SET profile_name = ? WHERE id = ?', (owner, sid)))
        return sid

    def snapshot(self):
        return (self.db.get_session(self.current.session_id), self.db.get_session('target'),
                self.db.load_gateway_routing_entries(scope=self.store._routing_scope()))

    def test_recovery_rejects_foreign_stamp_even_exact_route(self):
        self.seed(owner='foreign')
        self.assertIsNone(self.store._recover_session_from_db(
            session_key=self.key, source=self.source, now=_now()))
        self.assertIsNone(self.store._query_recoverable_session(session_key=self.key, source=self.source, now=_now()))

    def test_recovery_multiplex_uses_requested_profile_and_legacy_namespace(self):
        self.store.config.multiplex_profiles = True
        for owner, key, allowed in [('alpha', 'agent:beta:telegram:dm:x', True),
                                    ('beta', 'agent:alpha:telegram:dm:x', False),
                                    (None, 'agent:alpha:telegram:dm:x', True),
                                    (None, None, False)]:
            with self.subTest(owner=owner, key=key):
                self.assertEqual(self.store._recovered_row_allowed_for_active_profile(
                    requested_session_key='agent:alpha:telegram:dm:x',
                    recovered={'profile_name': owner, 'session_key': key}), allowed)

    def test_foreign_and_missing_switch_do_not_change_state(self):
        self.seed(owner='foreign')
        before = self.snapshot()
        self.assertIsNone(self.store.switch_session(self.key, 'target'))
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.store.switch_session(self.key, 'missing'))
        self.assertEqual(self.snapshot(), before)
        self.assertIs(self.store._entries[self.key], self.current)

    def test_atomic_switch_preserves_compression_lineage_and_reset_children(self):
        original = 'agent:main:telegram:dm:original'
        self.seed('ancestor', key=original)
        self.db.end_session('ancestor', 'compression')
        self.seed(key=original, parent='ancestor')
        self.db.end_session('target', 'session_reset')
        self.seed('reset_child', key=original, parent='target')
        switched = self.store.switch_session(self.key, 'target')
        self.assertEqual(switched.session_id, 'target')
        self.assertEqual(self.db.get_session(self.current.session_id)['end_reason'], 'session_switch')
        self.assertIsNone(self.db.get_session('target')['ended_at'])
        self.assertEqual(self.db.get_session('ancestor')['session_key'], self.key)
        self.assertEqual(self.db.get_session('ancestor')['end_reason'], 'compression')
        self.assertEqual(self.db.get_session('reset_child')['session_key'], original)
        self.assertEqual(json.loads(self.db.get_session('reset_child')['model_config'])['_reset_from'], 'target')
        routes = self.db.load_gateway_routing_entries(scope=self.store._routing_scope())
        self.assertEqual(json.loads(routes[self.key])['session_id'], 'target')

    def test_foreign_compression_ancestor_denies_entire_switch(self):
        self.seed('ancestor', owner='foreign')
        self.db.end_session('ancestor', 'compression')
        self.seed(parent='ancestor')
        before = self.snapshot()
        self.assertIsNone(self.store.switch_session(self.key, 'target'))
        self.assertEqual(self.snapshot(), before)

    def test_routing_failure_rolls_back_all_session_writes(self):
        self.seed()
        self.db.end_session('target', 'session_reset')
        before = self.snapshot()
        self.db._execute_write(lambda conn: conn.execute(
            "CREATE TRIGGER reject_switch BEFORE UPDATE ON gateway_routing "
            "BEGIN SELECT RAISE(ABORT, 'fixture routing rejection'); END"))
        self.assertIsNone(self.store.switch_session(self.key, 'target'))
        self.assertEqual(self.snapshot(), before)
        self.assertIs(self.store._entries[self.key], self.current)

    def test_delayed_full_snapshot_cannot_overwrite_successful_switch(self):
        self.seed()
        with self.store._lock:
            data, generation = self.store._snapshot_routing_locked()
        self.assertIsNotNone(self.store.switch_session(self.key, 'target'))
        self.store._persist_routing_data(data, generation)
        routes = self.db.load_gateway_routing_entries(scope=self.store._routing_scope())
        self.assertEqual(json.loads(routes[self.key])['session_id'], 'target')

    def test_mirror_failure_keeps_committed_primary_and_memory(self):
        self.seed()
        with patch.object(self.store, '_save_sessions_json', side_effect=OSError('fixture mirror')):
            self.assertIsNotNone(self.store.switch_session(self.key, 'target'))
        self.assertEqual(self.store._entries[self.key].session_id, 'target')
        routes = self.db.load_gateway_routing_entries(scope=self.store._routing_scope())
        self.assertEqual(json.loads(routes[self.key])['session_id'], 'target')

    def test_database_failure_before_authorization_leaves_route(self):
        self.seed()
        before = self.snapshot()
        with patch.object(self.db, '_execute_write', side_effect=OSError('fixture unavailable')):
            self.assertIsNone(self.store.switch_session(self.key, 'target'))
        self.assertEqual(self.snapshot(), before)

    def test_recovery_reopen_failure_does_not_publish_route(self):
        self.seed()
        self.db.end_session('target', 'agent_close')
        with self.store._lock:
            self.store._entries.pop(self.key)
            self.store._save()
        with patch.object(self.db, 'reopen_session', side_effect=OSError('fixture reopen')):
            with self.assertRaises(OSError):
                self.store._recover_session_from_db(
                    session_key=self.key, source=self.source, now=_now())
            with self.assertRaises(OSError):
                self.store.get_or_create_session(self.source)
        self.assertNotIn(self.key, self.store._entries)
        self.assertNotIn(self.key, self.db.load_gateway_routing_entries(scope=self.store._routing_scope()))

    def test_single_profile_legacy_ownerless_remains_resumable(self):
        self.seed(owner=None)
        self.db._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET session_key = NULL WHERE id = 'target'"))
        self.assertIsNotNone(self.store.switch_session(self.key, 'target'))
        self.assertIsNone(self.db.get_session('target')['profile_name'])

    def test_multiplex_ownerless_fails_closed(self):
        self.seed(owner=None)
        self.db._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET session_key = NULL WHERE id = 'target'"))
        self.store.config.multiplex_profiles = True
        before = self.snapshot()
        self.assertIsNone(self.store.switch_session(self.key, 'target'))
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
