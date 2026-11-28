"""Managed-worker finalization preserves only the exact parked attempt's CI wait."""
import hermes_bootstrap

import os

import pytest

from gateway import session_kanban
from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch

pytest_plugins = ["tests.hermes_cli.test_kanban_ci_continuation"]


def _bind(conn, task, run):
    context = {"db": str(kb.kanban_db_path()), "task_id": task, "run_id": run,
               "claim_lock": kb.get_task(conn, task).claim_lock}
    with kb.write_txn(conn):
        kb._append_event(conn, task, "worker_bound",
                         {"pid": os.getpid(), "claim_lock": context["claim_lock"]}, run_id=run)
    return context


def _resume(conn, task, run, binding):
    return kb.unblock_task(conn, task, expected_scheduled_run_id=run, expected_ci_wait=binding,
                           ci_result={**binding, "status": "completed", "conclusion": "success"})


@pytest.mark.parametrize("finish_after_resume", [False, True])
def test_native_successful_finalizer_preserves_one_same_owner_continuation(scenario, finish_after_resume):
    conn, task, run, binding = scenario
    context = _bind(conn, task, run)
    assert kb.schedule_task(conn, task, expected_run_id=run, ci_wait=binding)
    if finish_after_resume:
        assert _resume(conn, task, run, binding)
    session_kanban._record_worker_result(context, 0, "Parked for CI; end this run")
    assert session_kanban.worker_result(context["db"], context)["exit_code"] == 0
    if not finish_after_resume:
        assert _resume(conn, task, run, binding)
    assert dispatch.check_respawn_guard(conn, task) is None
    assert not _resume(conn, task, run, binding)
    accepted = []

    def accept(current, _workspace):
        assert current.id == task and current.assignee == "default"
        assert current.current_run_id != run
        accepted.append(current.current_run_id)
        return 99999999

    dispatch.dispatch_once(conn, max_in_progress=1, spawn_fn=accept)
    dispatch.dispatch_once(conn, max_in_progress=1, spawn_fn=accept)
    assert len(accepted) == 1
    assert dispatch.check_respawn_guard(conn, task) == "active_pr"


@pytest.mark.parametrize("change", ["run", "pid", "claim", "missing_bound", "missing_code", "malformed",
                                    "failed", "bool_code", "bool_pid", "unknown", "owner_roundtrip", "hold"])
@pytest.mark.parametrize("after_resume", [False, True])
def test_non_benign_finalization_never_grants_ci_continuation(scenario, change, after_resume):
    conn, task, run, binding = scenario
    context = _bind(conn, task, run) if change != "missing_bound" else {"claim_lock": "unbound"}
    assert kb.schedule_task(conn, task, expected_run_id=run, ci_wait=binding)
    if after_resume:
        assert _resume(conn, task, run, binding)
    payload = {"pid": os.getpid(), "claim_lock": context["claim_lock"], "exit_code": 0}
    event_run = run + 1 if change == "run" else run
    kind = "unknown_transition" if change == "unknown" else "worker_result"
    patches = {"pid": {"pid": os.getpid() + 1}, "claim": {"claim_lock": "other-claim"},
               "failed": {"exit_code": 1}, "bool_code": {"exit_code": False}, "bool_pid": {"pid": True}}
    payload.update(patches.get(change, {}))
    if change == "missing_code":
        del payload["exit_code"]
    if change == "malformed":
        payload = "not a receipt"
    if change == "owner_roundtrip":
        assert kb.assign_task(conn, task, "other")
        assert kb.assign_task(conn, task, "default")
    if change == "hold":
        if not after_resume:
            assert kb.unblock_task(conn, task)
        assert kb.block_task(conn, task, kind="needs_input", reason="Explicit hold")
    with kb.write_txn(conn):
        kb._append_event(conn, task, kind, payload, run_id=event_run)
    before = kb.get_task(conn, task)
    if not after_resume:
        assert not _resume(conn, task, run, binding)
    assert kb.get_task(conn, task) == before
    assert dispatch.check_respawn_guard(conn, task) is not None
