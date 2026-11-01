"""Newer goal controls must win over an in-flight judge result (#31/#27)."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from hermes_cli import goals
from hermes_state import SessionDB


@pytest.mark.parametrize('control', ['pause', 'pause-done', 'replace', 'clear', 'shared-pause',
                                   'shared-clear', 'shared-clear-done', 'shared-clear-wait', 'shared-clear-blocked',
                                   'criteria', 'criteria-limit', 'shared-criteria-limit', 'criteria-wait',
                                   'ready-wait', 'unchanged'])
def test_newer_goal_control_survives_inflight_judgement(tmp_path, monkeypatch, control):
    db = SessionDB(db_path=tmp_path / 'state.db')
    monkeypatch.setattr(goals, '_get_session_db', lambda: db)
    manager = goals.GoalManager('owner')
    manager.set('Finish the original approved request', max_turns=1 if control.endswith('criteria-limit') else 20)
    if control == 'ready-wait':
        manager.wait_for_seconds(600, 'await dependency')
        manager.state.waiting_until = 1.0
        goals.save_goal('owner', manager.state)
        ready_wake = manager.state.waiting_wake_id
    started, finish = Event(), Event()

    def judge(*args, **kwargs):
        started.set()
        assert finish.wait(5), 'the test must release the in-flight judge'
        verdict = {'pause-done': 'done', 'shared-clear-done': 'done', 'shared-clear-wait': 'wait',
                   'shared-clear-blocked': 'blocked'}.get(control, 'continue')
        return verdict, 'Original verdict', False, {'seconds': 600} if verdict == 'wait' else None, False

    monkeypatch.setattr(goals, 'judge_goal', judge)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(manager.evaluate_after_turn, 'Original progress')
            try:
                assert started.wait(5), 'the real manager must reach its judge'
                if control == 'ready-wait':
                    assert goals.GoalManager('owner').state.waiting_wake_id == ready_wake
                newer = manager if control.startswith('shared-') else goals.GoalManager('owner')
                action = 'clear' if control.startswith('shared-clear') else control
                action = 'pause' if action in ('pause-done', 'shared-pause') else action
                action = 'unchanged' if action == 'ready-wait' else action
                if 'criteria' in action:
                    if control == 'criteria-wait':
                        newer.wait_for_seconds(600, 'new dependency')
                    action = 'criteria'
                actions = {
                    'pause': lambda: newer.pause('user-paused'),
                    'replace': lambda: newer.set('Follow the newer authorized request'),
                    'clear': newer.clear,
                    'criteria': lambda: newer.add_subgoal('Include the updated requirement'),
                    'unchanged': lambda: None,
                }
                actions[action]()
            finally:
                finish.set()
            decision = future.result(timeout=5)
        current = goals.GoalManager('owner').state
        if control.endswith('criteria-limit'):
            assert not decision['should_continue'] and current.status == 'paused'
            assert current.turns_used == current.max_turns == 1
            assert current.subgoals == ['Include the updated requirement']
            return
        if control == 'criteria-wait':
            assert not decision['should_continue'] and current.status == 'active'
            assert current.waiting_until and current.turns_used == 1
            assert current.subgoals == ['Include the updated requirement']
            return
        if control in ('unchanged', 'criteria', 'ready-wait'):
            assert decision['should_continue'] and current.turns_used == 1
            if control == 'ready-wait':
                assert current.waiting_wake_id is None
            if control == 'criteria':
                assert 'Include the updated requirement' in decision['continuation_prompt']
                assert current.subgoals == ['Include the updated requirement']
            return
        assert not decision['should_continue'], 'the stale judge must not start another owner turn'
        if control in ('pause', 'pause-done', 'shared-pause'):
            assert current.status == 'paused' and current.paused_reason == 'user-paused'
        elif control == 'replace':
            assert current.goal == 'Follow the newer authorized request' and current.turns_used == 0
        else:
            assert current.status == 'cleared'
    finally:
        db.close()


def test_pause_after_committed_turn_charge_survives_old_done_verdict(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / 'state.db')
    monkeypatch.setattr(goals, '_get_session_db', lambda: db)
    manager = goals.GoalManager('owner')
    manager.set('Finish the original approved request')
    committed, finish = Event(), Event()
    native_save = goals.save_goal

    def save_then_pause(*args, **kwargs):
        result = native_save(*args, **kwargs)
        if kwargs.get('expected_state') is not None and not committed.is_set():
            committed.set()
            assert finish.wait(5), 'release the committed evaluation write'
        return result

    monkeypatch.setattr(goals, 'save_goal', save_then_pause)
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **kw: ('done', 'old verdict', False, None, False))
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(manager.evaluate_after_turn, 'Original progress')
            try:
                assert committed.wait(5), 'the real turn charge must be committed'
                manager.pause('user-paused')
            finally:
                finish.set()
            decision = future.result(timeout=5)
        current = goals.GoalManager('owner').state
        assert not decision['should_continue'] and decision['status'] == 'paused'
        assert current.status == 'paused' and current.paused_reason == 'user-paused'
    finally:
        db.close()
