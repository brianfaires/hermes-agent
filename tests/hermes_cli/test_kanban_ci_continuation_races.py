"""CI qualification and observation error fences survive interleaved native writers."""
pytest_plugins = ["tests.hermes_cli.test_kanban_ci_continuation"]

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch


def _resume(conn, task, run, binding):
    assert kb.schedule_task(conn, task, expected_run_id=run, ci_wait=binding)
    assert kb.unblock_task(conn, task, expected_scheduled_run_id=run,
                           ci_result={**binding, "status": "completed", "conclusion": "success"})


@pytest.mark.parametrize("change", ["comment", "hold_cycle"])
def test_changed_authority_after_preview_is_rechecked_inside_claim(scenario, monkeypatch, change):
    conn, task, run, binding = scenario
    _resume(conn, task, run, binding)
    original = kb.claim_task
    preclaim_runs = []

    def interleave(connection, task_id, **kwargs):
        with kbc.connect_closing() as other:
            if change == "comment":
                kb.add_comment(other, task_id, "default", "https://github.com/example/fixture/pull/9")
            else:
                assert kb.block_task(other, task_id, kind="needs_input", reason="Explicit hold")
                assert kb.unblock_task(other, task_id)
        preclaim_runs.extend(r.id for r in kb.list_runs(connection, task_id))
        return original(connection, task_id, **kwargs)

    monkeypatch.setattr(kb, "claim_task", interleave)
    result = dispatch.dispatch_once(conn, max_in_progress=1,
                                    spawn_fn=lambda *_: pytest.fail("Changed proof must not dispatch"))
    assert result.spawned == []
    assert kb.get_task(conn, task).status == "ready"
    assert [r.id for r in kb.list_runs(conn, task)] == preclaim_runs
    assert all(r.ended_at is not None for r in kb.list_runs(conn, task))


def test_owner_roundtrip_invalidates_ci_proof_not_authentic_handoff(scenario, monkeypatch):
    from hermes_cli.kanban_db_ci import qualifies_ci_resume
    conn, task, run, binding = scenario
    _resume(conn, task, run, binding)
    comment = conn.execute("SELECT id, body, created_at FROM task_comments WHERE task_id = ?", (task,)).fetchone()
    assert qualifies_ci_resume(conn, task, [comment])
    # Later-second trusted native handoffs have independent existing authority.
    # The CI proof must not authorize them, nor may this repair disable that guard rule.
    monkeypatch.setattr(dispatch.time, "time", lambda: comment["created_at"] + 5)
    assert kb.assign_task(conn, task, "other")
    assert kb.assign_task(conn, task, "default")
    assert not qualifies_ci_resume(conn, task, [comment])
    assert dispatch.check_respawn_guard(conn, task) is None


def test_error_result_owner_fence_is_checked_in_unblock_transaction(scenario):
    conn, task, run, binding = scenario
    assert kb.schedule_task(conn, task, expected_run_id=run, ci_wait=binding)
    with kbc.connect_closing() as other:
        assert kb.assign_task(other, task, "other")
    before = kb.get_task(conn, task)
    assert not kb.unblock_task(conn, task, expected_scheduled_run_id=run,
                               expected_ci_wait=binding, note="CI query failed; no success proof")
    assert kb.get_task(conn, task) == before
    assert len(kb.list_comments(conn, task)) == 1
