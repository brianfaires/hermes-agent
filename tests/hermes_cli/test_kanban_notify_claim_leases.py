"""Existing native subscriptions migrate and only their lease owner can settle."""
from pathlib import Path
import time
from types import SimpleNamespace

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


def test_legacy_subscription_migrates_and_stale_owner_cannot_settle_successor(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = tmp_path / 'kanban.db'
    key = dict(platform='telegram', chat_id='owned-chat')
    with kbc.connect_closing(db) as conn:
        task = kb.create_task(conn, title='Wait for verified evidence')
        key['task_id'] = task
        kbn.add_notify_sub(conn, **key, notifier_profile='owner', delivery_mode='wake')
        for column in ('notify_claim_token', 'notify_claim_from_event_id', 'notify_claim_expires',
                       'last_wake_event_id', 'last_diagnostic_wake_event_id'):
            conn.execute(f'ALTER TABLE kanban_notify_subs DROP COLUMN {column}')
        conn.commit()
    kbc.init_db(db)
    with kbc.connect_closing(db) as conn:
        assert kb.block_task(conn, task, kind='needs_input', reason='Need a deliverable')
        old, cursor, events = kbn.claim_unseen_events_for_sub(conn, **key, claim_token='first-owner',
                                                            kinds=['blocked', 'block_loop_detected'])
        assert [event.kind for event in events] == ['blocked']
        assert old < cursor
    future = int(time.time()) + 121
    monkeypatch.setattr(kbn, 'time', SimpleNamespace(time=lambda: future))
    with kbc.connect_closing(db) as conn:
        assert kb.unblock_task(conn, task)
        assert kb.block_task(conn, task, kind='needs_input', reason='Still needs operator decision')
        _old, new_cursor, events = kbn.claim_unseen_events_for_sub(
            conn, **key, claim_token='successor', kinds=['blocked', 'block_loop_detected'])
        assert [event.kind for event in events] == ['blocked', 'block_loop_detected']
        assert new_cursor > cursor
        kbn.advance_notify_cursor(conn, **key, new_cursor=cursor, claim_token='first-owner')
        assert not kbn.rewind_notify_cursor(conn, **key, old_cursor=old, claimed_cursor=cursor,
                                            claim_token='first-owner')
        assert kbn.claim_unseen_events_for_sub(conn, **key, claim_token='third')[2] == []
        kbn.advance_notify_cursor(conn, **key, new_cursor=new_cursor, claim_token='successor')
        assert kbn.claim_unseen_events_for_sub(conn, **key, claim_token='fourth')[2] == []
        sub, = kbn.list_notify_subs(conn, task)
        assert (sub['notifier_profile'], sub['delivery_mode']) == ('owner', 'wake')
        assert kb.get_task(conn, task).status == 'triage'
