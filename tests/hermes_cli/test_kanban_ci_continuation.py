"""Only exact native CI completion can continue the same owner against its existing PR."""
import hermes_bootstrap

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("{}\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title="Deliver existing PR", assignee="default",
                              workspace_kind="dir", workspace_path=str(tmp_path))
        claimed = kb.claim_task(conn, task)
        kb.add_comment(conn, task, "default", "Published https://github.com/example/fixture/pull/9")
        binding = {"repo": "example/fixture", "ci_run": 17, "pr": 9, "head_sha": "a" * 40}
        yield conn, task, claimed.current_run_id, binding


def test_ci_park_comment_failure_rolls_back_binding_and_attempt(scenario):
    import sqlite3

    conn, task, run, binding = scenario
    before = kb.get_task(conn, task)
    events = kb.list_events(conn, task_id=task)
    conn.execute("CREATE TRIGGER fail_ci_comment BEFORE INSERT ON task_comments "
                 "BEGIN SELECT RAISE(ABORT, 'fixture CI comment failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fixture CI comment failure"):
        kb.schedule_task(conn, task, reason="Exact CI", expected_run_id=run,
                         ci_wait=binding, comment_author="default")
    assert kb.get_task(conn, task) == before
    assert kb.list_events(conn, task_id=task) == events
    conn.execute("DROP TRIGGER fail_ci_comment")
    assert kb.schedule_task(conn, task, reason="Exact CI", expected_run_id=run,
                            ci_wait=binding, comment_author="default")
    assert kb.list_comments(conn, task)[-1].body == "SCHEDULED: Exact CI"
    assert kb.unblock_task(conn, task, expected_scheduled_run_id=run,
                           ci_result={**binding, "status": "completed", "conclusion": "success"})
    assert dispatch.check_respawn_guard(conn, task) is None


def test_successful_exact_ci_result_allows_one_same_owner_claim(scenario):
    conn, task, run, binding = scenario
    assert kb.schedule_task(conn, task, reason="Native CI wait", expected_run_id=run, ci_wait=binding)
    assert kb.unblock_task(conn, task, expected_scheduled_run_id=run,
                           ci_result={**binding, "status": "completed", "conclusion": "success"},
                           note="Exact CI success")
    assert dispatch.check_respawn_guard(conn, task) is None
    assert not kb.unblock_task(conn, task, expected_scheduled_run_id=run,
                               ci_result={**binding, "status": "completed", "conclusion": "success"})
    assert dispatch.dispatch_once(conn, max_in_progress=0, spawn_fn=lambda *_: pytest.fail("capacity")).spawned == []
    accepted = []

    def accept(current, _workspace):
        assert current.id == task and current.assignee == "default"
        assert current.current_run_id != run
        accepted.append(current.current_run_id)
        return 99999999

    dispatch.dispatch_once(conn, max_in_progress=1, spawn_fn=accept)
    dispatch.dispatch_once(conn, max_in_progress=1, spawn_fn=accept)
    assert len(accepted) == 1
    assert len(kb.list_comments(conn, task)) == 2
    # The completed wait cannot authorize another run, even before the guard's time window expires.
    assert dispatch.check_respawn_guard(conn, task) == "active_pr"


@pytest.mark.parametrize("change", [
    {"repo": "other/fixture"}, {"ci_run": 18}, {"pr": 10}, {"head_sha": "b" * 40},
    {"status": "in_progress"}, {"conclusion": ""}, {"head_sha": "abc"}, {"ci_run": True},
])
def test_mismatched_or_malformed_result_leaves_wait_unchanged(scenario, change):
    conn, task, run, binding = scenario
    assert kb.schedule_task(conn, task, expected_run_id=run, ci_wait=binding)
    before = kb.get_task(conn, task)
    with pytest.raises(ValueError):
        kb.unblock_task(conn, task, expected_scheduled_run_id=run,
                        ci_result={**binding, "status": "completed", "conclusion": "success", **change})
    assert kb.get_task(conn, task) == before
    assert len(kb.list_comments(conn, task)) == 1


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "neutral", "skipped"])
def test_non_success_result_is_honest_but_does_not_authorize_active_pr(scenario, conclusion):
    conn, task, run, binding = scenario
    assert kb.schedule_task(conn, task, expected_run_id=run, ci_wait=binding)
    assert kb.unblock_task(conn, task, expected_scheduled_run_id=run,
                           ci_result={**binding, "status": "completed", "conclusion": conclusion}, note=conclusion)
    assert conclusion in kb.build_worker_context(conn, task)
    assert dispatch.check_respawn_guard(conn, task) == "active_pr"


@pytest.mark.parametrize("change", ["legacy_wait", "missing_result", "generic_unblock", "new_pr", "new_pr_same_url",
                                    "hold_roundtrip", "stale_proof", "malformed_proof"])
def test_absent_stale_or_superseded_proof_keeps_duplicate_pr_guard(scenario, change, monkeypatch):
    conn, task, run, binding = scenario
    assert kb.schedule_task(conn, task, expected_run_id=run,
                            **({} if change == "legacy_wait" else {"ci_wait": binding}))
    if change in {"legacy_wait", "missing_result"}:
        assert kb.unblock_task(conn, task, expected_scheduled_run_id=run, note="CI success prose is not authority")
    else:
        assert kb.unblock_task(conn, task, expected_scheduled_run_id=run,
                               ci_result={**binding, "status": "completed", "conclusion": "success"})
        assert dispatch.check_respawn_guard(conn, task) is None
        if change == "generic_unblock" or change == "hold_roundtrip":
            assert kb.block_task(conn, task, kind="needs_input", reason="Explicit hold")
            assert kb.unblock_task(conn, task)
        elif change.startswith("new_pr"):
            kb.add_comment(conn, task, "default", "https://github.com/example/fixture/pull/" +
                           ("9" if change == "new_pr_same_url" else "10"))
        elif change == "stale_proof":
            import hermes_cli.kanban_db_ci as ci
            now = __import__("time").time()
            monkeypatch.setattr(ci.time, "time", lambda: now + dispatch._RESPAWN_GUARD_PR_WINDOW)
            # Keep the PR comment inside the current window; only the proof expires.
            with kb.write_txn(conn):
                conn.execute("UPDATE task_comments SET created_at = ? WHERE task_id = ?", (int(now) + 1, task))
        elif change == "malformed_proof":
            with kb.write_txn(conn):
                conn.execute("UPDATE task_events SET payload = ? WHERE task_id = ? AND kind = 'unblocked'",
                             ('{"ci_resume": {"wait": []}}', task))
    assert dispatch.check_respawn_guard(conn, task) == "active_pr"


@pytest.mark.parametrize("change", ["owner", "hold", "new_wait", "different_task"])
def test_result_cannot_cross_task_attempt_owner_or_explicit_hold(scenario, change):
    conn, task, run, binding = scenario
    assert kb.schedule_task(conn, task, expected_run_id=run, ci_wait=binding)
    if change == "owner":
        kb.assign_task(conn, task, "other")
    elif change == "hold":
        assert kb.unblock_task(conn, task)
        assert kb.block_task(conn, task, kind="needs_input", reason="Do not resume")
    elif change == "new_wait":
        assert kb.unblock_task(conn, task)
        assert kb.claim_task(conn, task)
        new_run = kb.get_task(conn, task).current_run_id
        assert kb.schedule_task(conn, task, expected_run_id=new_run, ci_wait=binding)
    else:
        task = kb.create_task(conn, title="Unrelated", assignee="default")
        assert kb.claim_task(conn, task)
        assert kb.schedule_task(conn, task)
    before = kb.get_task(conn, task)
    try:
        assert not kb.unblock_task(conn, task, expected_scheduled_run_id=run,
                                   ci_result={**binding, "status": "completed", "conclusion": "success"})
    except ValueError:
        pass
    assert kb.get_task(conn, task) == before


def test_result_and_proof_roll_back_together_on_failed_note(scenario, monkeypatch):
    import sqlite3
    conn, task, run, binding = scenario
    assert kb.schedule_task(conn, task, expected_run_id=run, ci_wait=binding)

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected note failure")

    monkeypatch.setattr(kb, "add_comment", fail)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        kb.unblock_task(conn, task, expected_scheduled_run_id=run,
                        ci_result={**binding, "status": "completed", "conclusion": "success"}, note="Result")
    assert kb.get_task(conn, task).status == "scheduled"
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'unblocked'", (task,)).fetchone()
