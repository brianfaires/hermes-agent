"""An external result may release only the parked attempt it was watching (#91)."""
import hermes_bootstrap

from pathlib import Path
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc


@pytest.fixture
def parked(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("{}\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title="Wait for CI", assignee="default",
                              workspace_kind="dir", workspace_path=str(tmp_path))
        assert kb.claim_task(conn, task)
        run = kb.get_task(conn, task).current_run_id
        assert kb.schedule_task(conn, task, reason="CI is running", expected_run_id=run)
        yield conn, task, run


def test_matching_scheduled_attempt_resumes_once(parked):
    conn, task, run = parked
    assert kb.unblock_task(conn, task, expected_scheduled_run_id=run, note="CI run 17 finished successfully")
    assert kb.get_task(conn, task).status == "ready"
    assert not kb.unblock_task(conn, task, expected_scheduled_run_id=run, note="Repeated result")
    assert [c.body for c in kb.list_comments(conn, task)] == ["CI run 17 finished successfully"]


def test_result_write_failure_keeps_the_task_parked(parked, monkeypatch):
    conn, task, run = parked

    def fail_comment(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic comment write failure")

    monkeypatch.setattr(kb, "add_comment", fail_comment)
    with pytest.raises(sqlite3.OperationalError, match="comment write failure"):
        kb.unblock_task(conn, task, expected_scheduled_run_id=run, note="CI finished")
    assert kb.get_task(conn, task).status == "scheduled"
    assert kb.list_comments(conn, task) == []


@pytest.mark.parametrize("change", ["newer_wait", "manual_hold", "live_run", "completed"])
def test_external_result_cannot_release_new_work_or_a_manual_hold(parked, change):
    conn, task, expected = parked
    assert kb.unblock_task(conn, task)
    if change == "manual_hold":
        assert kb.block_task(conn, task, kind="needs_input", reason="Brian's hold")
        assert kb.schedule_task(conn, task, reason="Preparation must not clear the hold")
        expected = conn.execute("SELECT run_id FROM task_events WHERE task_id = ? AND kind = 'scheduled' "
                                "ORDER BY id DESC LIMIT 1", (task,)).fetchone()[0]
    else:
        assert kb.claim_task(conn, task)
        run = kb.get_task(conn, task).current_run_id
        if change == "newer_wait":
            assert kb.schedule_task(conn, task, reason="Another CI run", expected_run_id=run)
        elif change == "completed":
            assert kb.complete_task(conn, task, summary="Already delivered", expected_run_id=run)
    before = kb.get_task(conn, task)
    comments = kb.list_comments(conn, task)
    assert not kb.unblock_task(conn, task, expected_scheduled_run_id=expected, note="Stale CI result")
    assert kb.get_task(conn, task) == before
    assert kb.list_comments(conn, task) == comments
    if change == "manual_hold":
        assert kb.unblock_task(conn, task), "An explicit operator release still works"
