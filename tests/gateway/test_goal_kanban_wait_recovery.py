"""A coordinator waits quietly, then accepts the changed Kanban attempt without a user nudge (#92)."""
import asyncio
import sys

import pytest

from gateway.run_goal_waits import poll_goal_waits
from gateway.session_results import execution_result
from hermes_cli import goals, kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_db_dispatch import _set_worker_pid
from tests.gateway import test_goal_wait_recovery as recovery

pytestmark = pytest.mark.platforms('linux')
isolated_home = recovery.isolated_home


@pytest.mark.asyncio
@pytest.mark.parametrize('transition', ['completed', 'blocked', 'scheduled', 'exited',
                                       'notified-completed', 'notified-scheduled', 'notifications-disabled',
                                       'notifications-unowned', 'notifications-late', 'notifications-claimed',
                                       'removed-board'])
async def test_coordinator_accepts_worker_result_without_spending_wait_budget(isolated_home, monkeypatch, transition):
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(isolated_home))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    runner, _adapter, authority, source, key, _executed, sid = await recovery.start_work(monkeypatch)
    worker = await asyncio.create_subprocess_exec(
        sys.executable, '-c', 'import sys; sys.stdin.buffer.read()', stdin=asyncio.subprocess.PIPE)
    observed = []

    def judge(_goal, response, **kwargs):
        processes = kwargs.get('background_processes') or []
        if response == 'Waiting on the assigned worker' and any(p['pid'] == worker.pid for p in processes):
            return 'wait', 'No independent step until the worker changes state', False, {'pid': worker.pid}, False
        if response == 'Observed blocked':
            return 'blocked', 'The assigned worker needs operator input', False, None, False
        return 'done', 'The owner accepted the changed task state', False, None, False

    async def owner_turn(event, source, quick_key, run_generation):
        status = 'missing'
        if kb.kanban_db_path().is_file():
            with kbc.connect_closing() as conn:
                task = kb.get_task(conn, task_id)
                status = task.status if task else 'missing'
        observed.append(status)
        text = 'Observed ' + status
        execution_result.get()['result'] = {'final_response': text, 'messages': []}
        return text

    monkeypatch.setattr(goals, 'judge_goal', judge)
    monkeypatch.setattr(runner, '_handle_message_with_agent', owner_turn)
    try:
        with kbc.connect_closing() as conn:
            task_id = kb.create_task(conn, title='Assigned implementation', assignee='default', session_id=sid,
                                     workspace_kind='dir', workspace_path=str(isolated_home))
            assert kb.claim_task(conn, task_id)
            _set_worker_pid(conn, task_id, worker.pid)
            run_id = kb.get_task(conn, task_id).current_run_id
            if transition.startswith('notifi'):
                from hermes_cli import kanban_db_notify as notify
                if transition != 'notifications-late':
                    notify.add_notify_sub(conn, task_id=task_id, platform='discord', chat_id='42',
                                          user_id='42', chat_type='dm', delivery_mode='wake')
                runner._kanban_notifier_enabled = transition != 'notifications-disabled'
                if transition in ('notified-completed', 'notified-scheduled', 'notifications-late', 'notifications-claimed'):
                    runner._kanban_dispatcher_lock_handle = object()
        manager = goals.GoalManager(sid)
        manager.set('Deliver the assigned change', max_turns=3)
        await runner._post_turn_goal_continuation(
            session_entry=runner.session_store.lookup_by_session_key(key), source=source,
            final_response='Waiting on the assigned worker')
        for _ in range(2):
            await poll_goal_waits(runner)
        parked = goals.GoalManager(sid)
        assert parked.state.turns_used == 1 and parked.is_waiting()
        assert parked.state.waiting_on_kanban.run_id == run_id
        assert observed == []
        if transition == 'exited':
            await asyncio.wait_for(worker.communicate(), 5)
        elif transition == 'removed-board':
            runner._kanban_notifier_enabled = True
            kb.kanban_db_path().unlink()
        else:
            with kbc.connect_closing() as conn:
                if transition in ('completed', 'notified-completed', 'notifications-disabled', 'notifications-unowned',
                                  'notifications-late', 'notifications-claimed'):
                    assert kb.complete_task(conn, task_id, summary='Implementation ready', expected_run_id=run_id)
                elif transition == 'blocked':
                    assert kb.block_task(conn, task_id, reason='Need operator input', kind='needs_input', expected_run_id=run_id)
                else:
                    assert kb.schedule_task(conn, task_id, reason='External CI wait', expected_run_id=run_id)
            assert worker.returncode is None, 'the board transition must release the wait before process exit'
        claimed = None
        if transition == 'notifications-late':
            with kbc.connect_closing() as conn:
                notify.add_notify_sub(conn, task_id=task_id, platform='discord', chat_id='42',
                                      user_id='42', chat_type='dm', delivery_mode='wake')
        if transition == 'notifications-claimed':
            from gateway.kanban_watchers_notifier import _notifier_collect
            claimed = await asyncio.to_thread(_notifier_collect, runner, kb,
                notifier_profile=None, gc_due=False, gc_retention_days=30)
            assert claimed
        await poll_goal_waits(runner)
        await recovery.settled(authority, sid)
        if transition in ('notified-completed', 'notifications-unowned', 'notifications-late', 'notifications-claimed'):
            from gateway.config import Platform
            from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
            deliveries = claimed if claimed is not None else await asyncio.to_thread(_notifier_collect, runner, kb,
                notifier_profile=None, gc_due=False, gc_retention_days=30)
            assert bool(deliveries) == (transition in ('notified-completed', 'notifications-claimed'))
            for delivery in deliveries:
                await _KanbanNotification(runner, delivery, platform_cls=Platform, sub_fail_counts={}).deliver()
            await recovery.settled(authority, sid)
        await poll_goal_waits(runner)
        expected = {'completed': 'done', 'notified-completed': 'done', 'exited': 'running',
                    'notified-scheduled': 'scheduled', 'notifications-disabled': 'done',
                    'notifications-unowned': 'done', 'notifications-late': 'done', 'notifications-claimed': 'done',
                    'removed-board': 'missing'}.get(transition, transition)
        assert observed == [expected]
        assert goals.GoalManager(sid).state.turns_used == 2
        if transition == 'blocked':
            with kbc.connect_closing() as conn:
                assert kb.get_task(conn, task_id).block_kind == 'needs_input'
            assert goals.GoalManager(sid).state.status == 'paused'
    finally:
        if worker.returncode is None:
            worker.kill()
        await asyncio.wait_for(worker.wait(), 5)
