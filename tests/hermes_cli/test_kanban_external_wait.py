"""External waiting uses native runs/events, including across reopened stores."""
import json
import os
from pathlib import Path
import time

import pytest
from hermes_cli import kanban_db as kb


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(kb, "kanban_home", lambda: tmp_path)
    return kb.board_db_path("default")


def setup_wait(conn):
    target = kb.create_task(conn, title="Separate external executor", assignee="ops")
    external = kb.claim_task(conn, target)
    card = kb.create_task(conn, title="Initiating controller", assignee="ang",
                          subscription_only=True, provider_override="openai-codex", model_override="gpt-6.1-sol")
    current = kb.claim_task(conn, card)
    wait = dict(next_owner="default", expected_effect="Relay verified Ops result",
                result_ref=dict(board="default", task_id=target, run_id=external.current_run_id),
                resume_condition="result", recheck_at=int(time.time()) + 60,
                executor="ops")
    return card, current.current_run_id, target, wait


def test_wait_closes_run_and_resumes_once_after_real_reopen(store):
    conn = kb.connect(store)
    card, run, target, wait = setup_wait(conn)
    conn.execute("UPDATE tasks SET consecutive_failures=2, block_recurrences=2, max_retries=5 WHERE id=?", (card,))
    conn.commit()
    assert kb.schedule_task(conn, card, reason="Ops owns external result", expected_run_id=run, wait=wait)
    assert kb.get_run(conn, run).outcome == "scheduled"
    assert kb.get_run(conn, run).metadata["external_wait"] == wait
    assert kb.get_task(conn, card).current_run_id is None
    assert kb.reconcile_external_waits(conn) == []
    assert kb.complete_task(conn, target, summary="Actual separate result")
    conn.close()
    with kb.connect_closing(store) as reopened:
        assert kb.reconcile_external_waits(reopened) == [card]
        resumed = kb.get_task(reopened, card)
        assert resumed.status == "ready" and resumed.assignee == "default"
        assert resumed.consecutive_failures == 2 and resumed.block_recurrences == 2
        assert resumed.subscription_only
        before = kb.list_events(reopened, card)
        assert kb.reconcile_external_waits(reopened) == []
        assert kb.reconcile_external_waits(reopened) == []
        assert kb.list_events(reopened, card) == before
        assert kb.goal_run_status(reopened, card, run) == "superseded"


def test_transport_and_deadline_do_not_complete_or_loop(store):
    with kb.connect_closing(store) as conn:
        card, run, target, wait = setup_wait(conn)
        assert kb.schedule_task(conn, card, reason="ops: result not yet verified", expected_run_id=run, wait=wait)
        with kb.write_txn(conn):
            kb._append_event(conn, target, "notification_delivered", {"accepted": True})
        for _ in range(2):
            assert kb.reconcile_external_waits(conn, now=wait["recheck_at"] + 1) == []
        task = kb.get_task(conn, card)
        assert task.status == "scheduled" and task.consecutive_failures == 0
        events = [e for e in kb.list_events(conn, card) if e.kind == "external_wait_due"]
        assert len(events) == 1
        assert events[0].payload["blocker"] == "ops: result not yet verified"
        assert events[0].payload["next_owner"] == "default"


@pytest.mark.parametrize("guard", ["hold", "stale_run", "wrong_executor", "self_reference"])
def test_wait_rejects_invalid_fences_atomically(store, guard):
    with kb.connect_closing(store) as conn:
        card, run, target, wait = setup_wait(conn)
        if guard == "hold":
            kb.block_task(conn, card, reason="ESTOP", kind="needs_input", expected_run_id=run)
        elif guard == "stale_run":
            run += 999
        elif guard == "wrong_executor":
            wait["executor"] = "somebody-else"
        else:
            wait["result_ref"].update(task_id=card, run_id=run)
            wait["executor"] = "ang"
        before = kb.list_events(conn, card)
        assert not kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
        assert kb.list_events(conn, card) == before


def test_resume_preserves_parent_and_manual_hold(store):
    with kb.connect_closing(store) as conn:
        card, run, target, wait = setup_wait(conn)
        assert kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
        kb.complete_task(conn, target, summary="result")
        with kb.write_txn(conn):
            kb._append_event(conn, card, "blocked", {"kind": "needs_input", "recurrences": 1})
        assert kb.reconcile_external_waits(conn) == []
        assert kb.get_task(conn, card).status == "scheduled"


@pytest.mark.parametrize("guard", ["estop", "failure_limit", "ownership_changed", "operator_rescheduled"])
def test_reconcile_preserves_stop_and_ownership_fences(store, monkeypatch, guard):
    from agent import estop
    with kb.connect_closing(store) as conn:
        card, run, target, wait = setup_wait(conn)
        assert kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
        kb.complete_task(conn, target, summary="result")
        if guard == "estop":
            monkeypatch.setattr(estop, "is_engaged", lambda: True)
        elif guard == "failure_limit":
            conn.execute("UPDATE tasks SET consecutive_failures=3, max_retries=3 WHERE id=?", (card,))
            conn.commit()
        elif guard == "ownership_changed":
            kb.assign_task(conn, card, "other-owner")
        else:
            kb.unblock_task(conn, card)
            kb.schedule_task(conn, card, reason="operator's separate schedule")
        assert kb.reconcile_external_waits(conn) == []
        assert kb.get_task(conn, card).status == "scheduled"


def test_same_receipt_cannot_create_another_resume(store):
    with kb.connect_closing(store) as conn:
        card, run, target, wait = setup_wait(conn)
        assert kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
        kb.complete_task(conn, target, summary="result")
        assert kb.reconcile_external_waits(conn) == [card]
        successor = kb.claim_task(conn, card)
        assert not kb.schedule_task(conn, card, reason="duplicate receipt", expected_run_id=successor.current_run_id, wait=wait)


def test_callable_tool_persists_wait_and_existing_metadata(store, monkeypatch):
    from tools.kanban_tools import _handle_block
    with kb.connect_closing(store) as conn:
        card, run, target, wait = setup_wait(conn)
        conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", ('{"existing_receipt":"keep"}', run))
        conn.commit()
        monkeypatch.setenv("HERMES_KANBAN_TASK", card)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run))
        monkeypatch.setenv("HERMES_KANBAN_DB", str(store))
        response = json.loads(_handle_block(dict(reason="Ops owns result", kind="dependency", wait=wait)))
        assert response["status"] == "scheduled", response
        saved = kb.get_run(conn, run)
        assert saved.metadata == {"existing_receipt": "keep", "external_wait": wait}
        bootstrap = kb.build_worker_context(conn, card)
        assert "External wait evidence, not approval" in bootstrap
        assert wait["expected_effect"] in bootstrap
        # A worker cannot repeat the mutation with its now-closed run.
        assert "error" in json.loads(_handle_block(dict(reason="repeat", kind="dependency", wait=wait)))


def test_cross_board_result_reconciles_after_process_restart(store):
    import subprocess
    import sys
    with kb.connect_closing(store) as conn:
        card, run, _, wait = setup_wait(conn)
        with kb.connect_closing(kb.board_db_path("operations")) as foreign:
            target = kb.create_task(foreign, title="Independent Ops result", assignee="ops")
            external = kb.claim_task(foreign, target)
            wait["result_ref"] = dict(board="operations", task_id=target, run_id=external.current_run_id)
            assert kb.schedule_task(conn, card, reason="Ops must return result", expected_run_id=run, wait=wait)
            kb.complete_task(foreign, target, summary="native result")
    # The initiator and both SQLite connections are gone. A fresh process
    # reconstructs the obligation from the existing stores, without a chat.
    code = "from hermes_cli import kanban_db as k; import json; c=k.connect(); print(json.dumps(k.reconcile_external_waits(c))); c.close()"
    env = dict(os.environ, HERMES_HOME=str(store.parent), HERMES_KANBAN_DB=str(store))
    first = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert json.loads(first.stdout.strip().splitlines()[-1]) == [card]
    second = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert json.loads(second.stdout.strip().splitlines()[-1]) == []


def test_dispatcher_reconciles_then_uses_normal_claim_once(store, monkeypatch):
    with kb.connect_closing(store) as conn:
        card, run, target, wait = setup_wait(conn)
        assert kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
        kb.complete_task(conn, target, summary="result")
        calls = []
        def spawn(task, workspace, **kwargs):
            calls.append((task.id, task.assignee, task.subscription_only))
            return os.getpid()
        # Host pressure is unrelated to the native lifecycle under test.
        monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "normal")
        for _ in range(3):
            kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, board="default")
        assert calls == [(card, "default", True)]
        assert kb.get_task(conn, card).status == "running"


def test_resume_regates_unfinished_parent(store):
    with kb.connect_closing(store) as conn:
        card, run, target, wait = setup_wait(conn)
        assert kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
        parent = kb.create_task(conn, title="Unfinished prerequisite", assignee="ops")
        kb.link_tasks(conn, parent, card)
        kb.complete_task(conn, target, summary="result")
        # A link is a substantive operator mutation; do not override it.
        assert kb.reconcile_external_waits(conn) == []
        assert kb.get_task(conn, card).status == "scheduled"


@pytest.mark.parametrize("condition", ["accepted", "launched"])
def test_receipt_condition_is_native_run_effect(store, condition):
    with kb.connect_closing(store) as conn:
        card, run, target, wait = setup_wait(conn)
        wait["resume_condition"] = condition
        assert kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
        if condition == "launched":
            # A native claim alone is not a launch receipt.
            assert kb.reconcile_external_waits(conn) == []
            kb._set_worker_pid(conn, target, os.getpid())
        assert kb.reconcile_external_waits(conn) == [card]
        assert kb.get_task(conn, card).status == "ready"


def test_external_failure_stays_owned_wait_and_goal_loop_does_not_infer(store, monkeypatch):
    from hermes_cli import goals
    def forbidden(*args, **kwargs):
        raise AssertionError("expected waiting must not infer, run another turn or block")
    monkeypatch.setattr(goals, "judge_goal", forbidden)
    with kb.connect_closing(store) as conn:
        card, run, target, wait = setup_wait(conn)
        assert kb.schedule_task(conn, card, reason="ops: requires successful result", expected_run_id=run, wait=wait)
        kb.block_task(conn, target, reason="real external failure", kind="capability")
        assert kb.reconcile_external_waits(conn, now=wait["recheck_at"] + 1) == []
        assert kb.get_task(conn, card).status == "scheduled"
        result = goals.run_kanban_goal_loop(
            task_id=card, goal_text="handoff", run_turn=forbidden, block_fn=forbidden,
            task_status_fn=lambda: kb.goal_run_status(conn, card, run))
        assert result["outcome"] == "stopped"
        assert kb.get_task(conn, card).consecutive_failures == 0


def test_wait_owner_is_a_profile_identity_not_a_path(store):
    with kb.connect_closing(store) as conn:
        card, run, target, wait = setup_wait(conn)
        wait["next_owner"] = "../ops"
        before = kb.list_events(conn, card)
        with pytest.raises(ValueError):
            kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
        assert kb.list_events(conn, card) == before


def test_unknown_wait_destination_rejected_before_any_mutation(store):
    with kb.connect_closing(store) as conn:
        card, run, _, wait = setup_wait(conn)
        wait["next_owner"] = "missing-relay-profile"
        before = kb.list_events(conn, card)
        with pytest.raises(ValueError, match="destination"):
            kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
        assert kb.list_events(conn, card) == before
        assert kb.get_task(conn, card).current_run_id == run


def test_unavailable_wait_destination_never_reassigned(store, monkeypatch):
    from hermes_cli import profiles
    with kb.connect_closing(store) as conn:
        card, run, target, wait = setup_wait(conn)
        assert kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
        kb.complete_task(conn, target, summary="result")
        # Availability can change between scheduling and reconciliation.
        monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
        before = kb.list_events(conn, card)
        assert kb.reconcile_external_waits(conn) == []
        assert kb.get_task(conn, card).assignee == "ang"
        assert kb.list_events(conn, card) == before
