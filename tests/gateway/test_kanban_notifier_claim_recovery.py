"""A crashed notifier claim is recoverable without polling unchanged waits into turns."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_agent
from gateway.config import Platform
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tests.gateway.test_kanban_wake_acceptance import setup_route, drain


def _collect(runner):
    return _notifier_collect(runner, kb, notifier_profile=None, gc_due=False, gc_retention_days=30)


async def _prepare(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_DB', str(tmp_path / 'board.db'))
    runner, adapter, _source, key = setup_route()
    received = []

    async def handler(event):
        received.append(event.text)

    adapter.set_message_handler(handler)
    await adapter.connect()
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title='Examined wait', session_id=key)
        successor = kb.create_task(conn, title='Unlaunched successor')
        kbn.add_notify_sub(conn, task_id=task, platform='telegram', chat_id='42', user_id='42',
                           delivery_mode='wake')
        assert kb.block_task(conn, task, reason='Waiting for actual evidence', kind='needs_input')
    return runner, adapter, received, task, successor


@pytest.mark.asyncio
async def test_restart_reclaims_abandoned_wake_then_unchanged_checks_are_quiet(tmp_path, monkeypatch):
    runner, adapter, received, task, successor = await _prepare(tmp_path, monkeypatch)
    try:
        first = _collect(runner)
        assert len(first) == 1
        # Drop the delivery and all collector connections: the durable claim alone survives.
        for _ in range(3):
            assert _collect(runner) == []
        assert received == []
        future = int(time.time()) + 121
        monkeypatch.setattr(kbn, 'time', SimpleNamespace(time=lambda: future))
        recovered = _collect(runner)
        assert len(recovered) == 1, 'crash before admission must not permanently lose the wake'
        assert recovered[0]['task'].id == task
        await _KanbanNotification(runner, recovered[0], platform_cls=Platform, sub_fail_counts={}).deliver()
        await drain(adapter)
        assert len(received) == 1
        monkeypatch.setattr(kbn, 'time', SimpleNamespace(time=lambda: future + 240))
        for _ in range(3):
            assert _collect(runner) == []
        assert len(received) == 1
        with kbc.connect_closing() as conn:
            assert kb.get_task(conn, task).status == 'blocked'
            assert kb.get_task(conn, successor).status == 'ready'
            assert kb.complete_task(conn, task, summary='The actual prerequisite evidence arrived.')
        changed = _collect(runner)
        assert len(changed) == 1
        await _KanbanNotification(runner, changed[0], platform_cls=Platform, sub_fail_counts={}).deliver()
        await drain(adapter)
        assert len(received) == 2
        assert _collect(runner) == []
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_stale_delivery_failure_cannot_rewind_a_new_claim(tmp_path, monkeypatch):
    runner, adapter, received, _task, _successor = await _prepare(tmp_path, monkeypatch)
    try:
        old, = _collect(runner)
        future = int(time.time()) + 121
        monkeypatch.setattr(kbn, 'time', SimpleNamespace(time=lambda: future))
        current, = _collect(runner)
        runner._kanban_rewind(old['sub'], old['cursor'], old['old_cursor'], old['board'])
        assert _collect(runner) == [], 'stale owner cannot release its successor claim'
        await _KanbanNotification(runner, current, platform_cls=Platform, sub_fail_counts={}).deliver()
        await drain(adapter)
        runner._kanban_advance(old['sub'], old['cursor'], old['board'])
        assert _collect(runner) == []
        assert len(received) == 1
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_partial_group_admission_does_not_repeat_examined_diagnostic(tmp_path, monkeypatch):
    from gateway import warning_notifications
    from gateway.wake import WakeNotAccepted

    runner, adapter, received, task, _successor = await _prepare(tmp_path, monkeypatch)
    try:
        with kbc.connect_closing() as conn:
            assert kb.unblock_task(conn, task)
            assert kb.block_task(conn, task, reason='Provider needs attention', kind='capability')
        monkeypatch.setattr(warning_notifications, 'warning_notifications_enabled', lambda *a: False)
        native_wake = _KanbanNotification.wake
        attempts = []

        async def reject_second_group_once(notification):
            attempts.append(notification.wake_diagnostic)
            if len(attempts) == 2:
                raise WakeNotAccepted('fixture second-group admission unavailable')
            await native_wake(notification)

        monkeypatch.setattr(_KanbanNotification, 'wake', reject_second_group_once)
        first, = _collect(runner)
        await _KanbanNotification(runner, first, platform_cls=Platform, sub_fail_counts={}).deliver()
        await drain(adapter)
        assert len(received) == 1
        retry, = _collect(runner)
        await _KanbanNotification(runner, retry, platform_cls=Platform, sub_fail_counts={}).deliver()
        await drain(adapter)
        assert attempts.count(True) == 1, 'an admitted diagnostic is already examined'
        assert len(received) == 2
        assert _collect(runner) == []
    finally:
        await adapter.disconnect()
