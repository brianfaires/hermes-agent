"""Scheduling refreshes a parked reason without inventing a new attempt."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("{}\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
                 "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(name, raising=False)
    with kbc.connect_closing() as conn:
        yield conn


def snapshot(conn, table):
    return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def test_scheduled_reason_refresh_preserves_attempt(board):
    conn = board
    tid = kb.create_task(conn, title="Wait", assignee="default")
    assert kb.claim_task(conn, tid)
    run = kb.get_task(conn, tid).current_run_id
    assert kb.schedule_task(conn, tid, reason="Old condition", expected_run_id=run)
    task_before = kb.get_task(conn, tid)
    runs_before = snapshot(conn, "task_runs")

    assert kb.schedule_task(conn, tid, reason="A concrete replacement condition")

    assert kb.get_task(conn, tid) == task_before
    assert snapshot(conn, "task_runs") == runs_before
    latest = kb.list_events(conn, task_id=tid)[-1]
    assert latest.kind == "scheduled"
    assert latest.payload["reason"] == "A concrete replacement condition"
    assert latest.run_id == run
    before = snapshot(conn, "task_events")
    assert kb.schedule_task(conn, tid, reason="A concrete replacement condition")
    assert snapshot(conn, "task_events") == before
    assert snapshot(conn, "task_runs") == runs_before


def test_failed_cli_schedule_does_not_record_success(board):
    from hermes_cli import kanban as kc

    tid = kb.create_task(board, title="Already done", assignee="default")
    assert kb.complete_task(board, tid, summary="Done", force=True)
    before = {table: snapshot(board, table) for table in ("tasks", "task_runs", "task_events", "task_comments")}
    output = kc.run_slash(f"schedule {tid} A misleading success reason")
    assert "cannot schedule" in output
    assert {table: snapshot(board, table) for table in before} == before


@pytest.mark.parametrize("initial", ["ready", "todo", "running", "blocked"])
def test_previous_schedule_entry_statuses_still_work(board, initial):
    tid = kb.create_task(board, title="Wait", assignee="default")
    if initial == "todo":
        parent = kb.create_task(board, title="Parent", assignee="default")
        kb.link_tasks(board, parent_id=parent, child_id=tid)
    elif initial == "running":
        assert kb.claim_task(board, tid)
    elif initial == "blocked":
        assert kb.block_task(board, tid, reason="External wait")
    assert kb.get_task(board, tid).status == initial
    assert kb.schedule_task(board, tid, reason="Specific external result")
    assert kb.get_task(board, tid).status == "scheduled"
    assert kb.latest_run(board, tid).outcome == "scheduled"


@pytest.mark.parametrize("initial", ["triage", "review", "done", "archived", "missing"])
def test_cli_refusals_have_no_partial_lifecycle(board, initial):
    from hermes_cli import kanban as kc

    tid = kb.create_task(board, title="Refuse", assignee="default", triage=initial == "triage")
    transitions = {
        "review": lambda: kb.request_review(board, tid, summary="Review", force=True),
        "done": lambda: kb.complete_task(board, tid, summary="Done", force=True),
        "archived": lambda: kb.archive_task(board, tid),
    }
    if initial in transitions:
        assert transitions[initial]()
    if initial == "missing":
        tid = "t_missing_fixture"
    tables = ("tasks", "task_runs", "task_events", "task_comments")
    before = {table: snapshot(board, table) for table in tables}
    output = kc.run_slash(f"schedule {tid} Refused condition")
    assert "cannot schedule" in output
    assert {table: snapshot(board, table) for table in tables} == before


@pytest.mark.parametrize("conflict", ["stale_run", "claim_lock", "claim_expires", "worker_pid", "current_run_id"])
def test_refresh_refuses_claim_conflicts_without_writes(board, conflict, monkeypatch):
    from hermes_cli import kanban as kc

    tid = kb.create_task(board, title="Wait", assignee="default")
    assert kb.claim_task(board, tid)
    run = kb.get_task(board, tid).current_run_id
    assert kb.schedule_task(board, tid, reason="Original", expected_run_id=run)
    if conflict == "stale_run":
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run))
    else:
        # Deliberately inconsistent disposable fixture: refresh must not repair it.
        with kb.write_txn(board):
            board.execute(f"UPDATE tasks SET {conflict} = ? WHERE id = ?", (run, tid))
    tables = ("tasks", "task_runs", "task_events", "task_comments")
    before = {table: snapshot(board, table) for table in tables}
    assert "cannot schedule" in kc.run_slash(f"schedule {tid} New condition")
    assert {table: snapshot(board, table) for table in tables} == before


@pytest.mark.parametrize("failure", ["comment", "event"])
@pytest.mark.parametrize("parked", [False, True])
def test_cli_recording_failure_rolls_back_schedule(board, parked, failure):
    from hermes_cli import kanban as kc

    tid = kb.create_task(board, title="Wait", assignee="default")
    assert kb.claim_task(board, tid)
    if parked:
        assert kb.schedule_task(board, tid, reason="Original")
    before = {table: snapshot(board, table) for table in ("tasks", "task_runs", "task_events", "task_comments")}
    # Abort actual recording writes, not a mocked transition result.
    table = {"comment": "task_comments", "event": "task_events"}[failure]
    board.execute(f"CREATE TRIGGER fail_record BEFORE INSERT ON {table} "
                  "BEGIN SELECT RAISE(ABORT, 'fixture recording failure'); END")
    assert "fixture recording failure" in kc.run_slash(f"schedule {tid} Replacement")
    assert {table: snapshot(board, table) for table in before} == before


def test_cli_refresh_is_atomic_and_matching_retry_is_a_noop(board, tmp_path):
    from hermes_cli import kanban as kc
    from hermes_cli.kanban_db_notify import add_notify_sub

    parent = kb.create_task(board, title="Parent", assignee="default")
    tid = kb.create_task(board, title="Wait", assignee="default", parents=[parent],
                         workspace_kind="dir", workspace_path=str(tmp_path),
                         model_override="fixture-model", provider_override="fixture-provider",
                         session_id="fixture-controller", max_runtime_seconds=90)
    child = kb.create_task(board, title="Child", assignee="default", parents=[tid])
    add_notify_sub(board, task_id=tid, platform="discord", chat_id="fixture-chat",
                   notifier_profile="default", delivery_metadata={"controller": "fixture"})
    assert kb.schedule_task(board, tid, reason="Original")
    tables = ("tasks", "task_runs", "task_links", "kanban_notify_subs")
    before = {table: snapshot(board, table) for table in tables}
    assert "Scheduled" in kc.run_slash(f"schedule {tid} Concrete replacement")
    assert {table: snapshot(board, table) for table in tables} == before
    assert [c.body for c in kb.list_comments(board, tid)] == ["SCHEDULED: Concrete replacement"]
    assert kb.get_task(board, child).status == "todo"
    after = {table: snapshot(board, table) for table in (*tables, "task_comments", "task_events")}
    assert "Scheduled" in kc.run_slash(f"schedule {tid} Concrete replacement")
    assert {table: snapshot(board, table) for table in after} == after


def test_refresh_preserves_scheduled_resume_consumer(board):
    tid = kb.create_task(board, title="Wait", assignee="default")
    assert kb.claim_task(board, tid)
    run = kb.get_task(board, tid).current_run_id
    assert kb.schedule_task(board, tid, reason="Original", expected_run_id=run)
    assert kb.schedule_task(board, tid, reason="More precise condition")
    assert kb.unblock_task(board, tid, expected_scheduled_run_id=run, note="Matching result")
    assert kb.get_task(board, tid).status == "ready"


@pytest.mark.parametrize("reason", [None, ""])
def test_refresh_without_reason_remains_idempotent(board, reason):
    tid = kb.create_task(board, title="Wait", assignee="default")
    assert kb.schedule_task(board, tid, reason="Original")
    assert kb.schedule_task(board, tid, reason=reason)
    before = {table: snapshot(board, table) for table in ("tasks", "task_runs", "task_events")}
    assert kb.schedule_task(board, tid, reason=reason)
    assert {table: snapshot(board, table) for table in before} == before
