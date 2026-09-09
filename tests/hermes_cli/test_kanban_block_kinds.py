"""Tests for typed block reasons + the unblock-loop breaker.

Covers the built-in fix for the kanban "blocked loop" — a worker blocks a
task, a cron unblocks it, the worker re-blocks for the same reason, repeat
forever. The fix gives ``block_task`` a typed ``kind`` and a persistent
``block_recurrences`` counter:

* ``dependency`` blocks route to ``todo`` (parent-gated, auto-resumed) and
  never enter the human ``blocked`` bucket a cron would keep unblocking.
* ``needs_input`` / ``capability`` / un-typed blocks land in ``blocked``;
  each same-cause re-block after an unblock increments ``block_recurrences``,
  and at ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
* ``unblock_task`` deliberately does NOT reset ``block_recurrences`` (the
  amnesia that let the loop run unbounded).
* A successful ``complete_task`` resets the loop memory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_specify as spec


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    """Create a task and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def _append_raw_event(conn, task_id: str, kind: str, payload: object) -> None:
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, 1)",
        (
            task_id,
            kind,
            json.dumps(payload) if isinstance(payload, (dict, list)) else payload,
        ),
    )


# ---------------------------------------------------------------------------
# Loop breaker
# ---------------------------------------------------------------------------










def test_block_loop_detected_event_emitted(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "block_loop_detected"]
        assert events, "expected a block_loop_detected event"
        payload = events[-1].payload or {}
        assert payload.get("recurrences") == 2
        assert payload.get("kind") == "capability"


def test_needs_input_loop_hold_is_not_auto_specified_decomposed_or_claimed(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="needs consent")
        assert kb.block_task(conn, tid, reason="ask the human", kind="needs_input")
        assert kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        assert kb.block_task(conn, tid, reason="ask the human", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"

    assert tid not in spec.list_triage_ids()
    assert tid not in decomp.list_triage_ids()

    specify_outcome = spec.specify_task(tid, author="auto-specifier")
    decompose_outcome = decomp.decompose_task(tid, author="auto-decomposer")
    assert specify_outcome.ok is False
    assert "explicit unblock" in specify_outcome.reason
    assert decompose_outcome.ok is False
    assert "explicit unblock" in decompose_outcome.reason

    with kb.connect_closing() as conn:
        assert kb.specify_triage_task(
            conn,
            tid,
            title="Proceed without consent",
            body="This must not apply.",
            author="test",
        ) is False
        assert kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="worker",
            children=[{"title": "child", "assignee": "worker"}],
            author="test",
        ) is None

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        monkeypatch.setattr(
            "hermes_cli.profiles.profile_exists", lambda _profile: True
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: pytest.fail(
                "dispatcher must not spawn a held task"
            ),
            max_in_progress=None,
        )
        assert result.spawned == []
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "triage"

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid, claimer="bypass") is None
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "triage"


def test_first_needs_input_hold_survives_drag_to_triage(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="needs first answer")
        assert kb.block_task(conn, tid, reason="ask the human", kind="needs_input")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
            _append_raw_event(conn, tid, "status", {"status": "triage"})

        assert kb.has_active_control_hold(conn, tid) is True
        assert kb.specify_triage_task(
            conn,
            tid,
            title="Automated spec must not proceed",
            body="No human release happened.",
            author="test",
        ) is False
        assert kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="worker",
            children=[{"title": "child", "assignee": "worker"}],
            author="test",
        ) is None


def test_specified_and_decomposed_events_do_not_release_control_hold(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="automation is not consent")
        assert kb.block_task(conn, tid, reason="ask the human", kind="needs_input")
        assert kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        assert kb.block_task(conn, tid, reason="ask the human", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"
        with kb.write_txn(conn):
            _append_raw_event(conn, tid, "specified", {"changed_fields": ["body"]})
            _append_raw_event(conn, tid, "decomposed", {"child_ids": ["t_fake"]})

        assert kb.has_active_control_hold(conn, tid) is True


def test_status_noise_and_event_volume_do_not_release_control_hold(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="status noise")
        assert kb.block_task(conn, tid, reason="ask the human", kind="needs_input")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
            for _ in range(25):
                _append_raw_event(conn, tid, "status", {"status": "triage"})
            _append_raw_event(conn, tid, "status", "{malformed")
            _append_raw_event(conn, tid, "status", {"status": "todo"})

        assert kb.has_active_control_hold(conn, tid) is True


def test_older_release_event_does_not_bypass_newer_typed_hold(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="newer hold")
        assert kb.block_task(conn, tid, reason="first question", kind="needs_input")
        assert kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        assert kb.block_task(conn, tid, reason="new capability wall", kind="capability")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
            _append_raw_event(conn, tid, "status", {"status": "triage"})

        assert kb.has_active_control_hold(conn, tid) is True


def test_explicit_unblock_releases_needs_input_triage_hold(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="release consent hold")
        assert kb.block_task(conn, tid, reason="ask the human", kind="needs_input")
        assert kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        assert kb.block_task(conn, tid, reason="ask the human", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"

        assert kb.unblock_task(conn, tid)
        released = kb.get_task(conn, tid)
        assert released is not None
        assert released.status == "ready"
        assert kb.claim_task(conn, tid, claimer="released") is not None


def test_explicit_promote_releases_stale_block_kind_hold(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="manual promote release")
        assert kb.block_task(conn, tid, reason="ask the human", kind="needs_input")
        promoted, error = kb.promote_task(
            conn, tid, actor="test-operator", force=True,
        )
        assert promoted and error is None

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))

        assert kb.has_active_control_hold(conn, tid) is False
        assert kb.specify_triage_task(
            conn,
            tid,
            title="Released stale hold",
            body="Manual promote released the prior hold.",
            author="test",
        ) is True


def test_legacy_triage_hold_uses_release_event_not_stale_block_kind(
    kanban_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        held = kb.create_task(conn, title="legacy held", assignee="worker", triage=True)
        released = kb.create_task(
            conn, title="legacy released", assignee="worker", triage=True
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET block_kind = 'needs_input', block_recurrences = ? "
                "WHERE id IN (?, ?)",
                (kb.BLOCK_RECURRENCE_LIMIT, held, released),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'unblocked', ?, ?)",
                (released, '{"status": "ready"}', 1),
            )

        assert kb.specify_triage_task(
            conn, held, title="Held", body="must stay held", author="test"
        ) is False
        assert kb.specify_triage_task(
            conn, released, title="Released", body="explicitly released", author="test"
        ) is True
        assert kb.get_task(conn, released).status == "ready"


# ---------------------------------------------------------------------------
# Dependency routing
# ---------------------------------------------------------------------------


def test_dependency_then_parent_done_promotes(kanban_home: Path) -> None:
    """A dependency-parked child becomes ready once its parent completes."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.block_task(conn, child, reason="wait", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        # Finish the parent, then let recompute_ready run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# Completion resets loop memory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------
