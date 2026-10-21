"""A terminal failure batch must not promise an automatic retry after giving up."""
from pathlib import Path

import pytest

import run_agent
from gateway.config import Platform
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_notify as notify
from tests.gateway.test_kanban_wake_acceptance import setup_route, drain


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['crashed', 'timed_out'])
async def test_gave_up_batch_reports_stopped_and_blocked_without_retry_promise(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    monkeypatch.setenv('HERMES_LANGUAGE', 'en')
    runner, adapter, _source, key = setup_route()
    received = []

    async def model_boundary(event):
        received.append(event.text)

    adapter.set_message_handler(model_boundary)
    await adapter.connect()
    try:
        with kbc.connect_closing() as conn:
            task = kb.create_task(conn, title='Failure budget exhausted', session_id=key)
            kb.claim_task(conn, task)
            notify.add_notify_sub(conn, task_id=task, platform='telegram', chat_id='42',
                                   user_id='42', delivery_mode='notify+wake')
            with kb.write_txn(conn):
                kb._append_event(conn, task, outcome, {'pid': 123, 'limit_seconds': 60})
            assert dispatch._record_task_failure(conn, task, 'Actual attempt failed', outcome=outcome,
                                                  failure_limit=1, release_claim=True, end_run=True)
            assert kb.get_task(conn, task).status == 'blocked'
        batch, = _notifier_collect(runner, kb, notifier_profile=None, gc_due=False, gc_retention_days=30)
        await _KanbanNotification(runner, batch, platform_cls=Platform, sub_fail_counts={}).deliver()
        await drain(adapter)
        text = '\n'.join(str(entry) for entry in adapter.wire) + '\n' + '\n'.join(received)
        assert 'blocked' in text.lower()
        assert 'retried automatically' not in text.lower()
        assert 'will retry' not in text.lower()
        assert received and adapter.wire
        with kbc.connect_closing() as conn:
            assert kb.get_task(conn, task).status == 'blocked'
            assert kb.claim_task(conn, task) is None
    finally:
        await adapter.disconnect()
