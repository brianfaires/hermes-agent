from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tools import kanban_tools


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="original", assignee="task-owner")
    return tid


def _instruction(conn, tid, assignee="instruction-owner"):
    return kb.create_owner_instruction(
        conn, task_id=tid, assignee=assignee, source="test",
        source_key=f"key-{time.time_ns()}", actor="owner", body="please inspect",
    )


def test_instruction_heartbeat_only_renews_instruction_lease(board, monkeypatch):
    with kb.connect() as conn:
        inst = kb.claim_owner_instruction(conn, _instruction(conn, board).id, ttl_seconds=10)
        assert inst is not None
        before_task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (board,)).fetchone())
        before_runs = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
    monkeypatch.setenv("HERMES_KANBAN_TASK", board)
    monkeypatch.setenv("HERMES_KANBAN_OWNER_INSTRUCTION", str(inst.id))
    monkeypatch.setenv("HERMES_KANBAN_OWNER_INSTRUCTION_CLAIM", inst.claim_lock)
    result = json.loads(kanban_tools._handle_heartbeat({"task_id": board}))
    assert result["ok"] is True
    with kb.connect() as conn:
        after_task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (board,)).fetchone())
        assert after_task == before_task
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == before_runs
        assert kb.get_owner_instruction(conn, inst.id).claim_expires >= inst.claim_expires


def test_expired_instruction_claim_cannot_finish_and_live_pid_is_not_requeued(board, monkeypatch):
    with kb.connect() as conn:
        inst = kb.claim_owner_instruction(conn, _instruction(conn, board).id, ttl_seconds=10)
        conn.execute("UPDATE task_owner_instructions SET claim_expires=?,worker_pid=? WHERE id=?",
                     (int(time.time()) - 1, os.getpid(), inst.id))
        with pytest.raises(ValueError, match="not active"):
            kb.finish_owner_instruction(conn, inst.id, inst.claim_lock, "completed", "done", author="worker")
        assert kb.release_stale_owner_instructions(conn) == 0
        assert kb.get_owner_instruction(conn, inst.id).status == "accepted"
        conn.execute("UPDATE task_owner_instructions SET claim_expires=? WHERE id=?",
                     (int(time.time()) + 100, inst.id))
        monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
        assert kb.release_stale_owner_instructions(conn) == 1
        assert kb.get_owner_instruction(conn, inst.id).status == "pending"


def test_legacy_accepted_instruction_does_not_consume_normal_worker_capacity(board, monkeypatch):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda profile: True)
    with kb.connect() as conn:
        inst = kb.claim_owner_instruction(conn, _instruction(conn, board, "same").id)
        kb.set_owner_instruction_pid(conn, inst.id, inst.claim_lock, os.getpid())
        kb.create_task(conn, title="task", assignee="same", triage=False)
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 123,
                         max_in_progress=1, max_in_progress_per_profile=1)
        assert len(spawned) == 1


def test_running_instruction_is_durably_queued_then_reopens_after_run(board):
    with kb.connect() as conn:
        claimed = kb.claim_task(conn, board)
        assert claimed is not None
        inst = _instruction(conn, board)
        assert kb.route_owner_instruction(conn, inst.id) == "queued"
        assert kb.get_task(conn, board).status == "running"
        assert kb.get_task(conn, board).claim_lock == claimed.claim_lock
        conn.execute(
            "UPDATE tasks SET status='done',claim_lock=NULL,claim_expires=NULL,worker_pid=NULL WHERE id=?",
            (board,),
        )
        assert kb.route_queued_owner_instructions(conn) == 1
        assert kb.get_task(conn, board).status == "ready"
        assert kb.get_owner_instruction(conn, inst.id).status == "routed"


def test_terminal_card_only_reopens_for_explicit_rerun(board):
    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (board,))
        ambiguous = _instruction(conn, board)
        assert kb.route_owner_instruction(conn, ambiguous.id) == "ignored"
        assert kb.get_task(conn, board).status == "done"
        rerun = _instruction(conn, board)
        assert kb.route_owner_instruction(conn, rerun.id, explicit_rerun=True) == "routed"
        assert kb.get_task(conn, board).status == "ready"


def test_blocked_instruction_respects_unfinished_dependencies(board):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="task-owner")
        kb.link_tasks(conn, parent, board)
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (board,))
        inst = _instruction(conn, board)
        assert kb.route_owner_instruction(conn, inst.id) == "routed"
        assert kb.get_task(conn, board).status == "todo"


def test_instruction_worker_cannot_create_cards(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", board)
    monkeypatch.setenv("HERMES_KANBAN_OWNER_INSTRUCTION", "42")
    with kb.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    result = json.loads(kanban_tools._handle_create({"title": "no", "assignee": "x"}))
    assert "instruction workers cannot create cards" in result["error"]
    with kb.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before


def test_completed_close_instruction_archives_original_card(board):
    with kb.connect() as conn:
        inst = kb.create_owner_instruction(
            conn,
            task_id=board,
            assignee="instruction-owner",
            source="test",
            source_key=f"close-{time.time_ns()}",
            actor="owner",
            body="Instruction: close_request\nMeaning: close it",
        )
        claimed = kb.claim_owner_instruction(conn, inst.id)
        assert claimed is not None
        assert claimed.claim_lock is not None
        kb.finish_owner_instruction(
            conn, claimed.id, claimed.claim_lock, "completed", "closed", author="worker"
        )
        assert kb.get_task(conn, board).status == "archived"
        assert kb.get_owner_instruction(conn, claimed.id).status == "completed"
        assert any(event.kind == "archived" for event in kb.list_events(conn, board))


def test_actionable_instruction_routes_review_card_back_to_normal_worker(board):
    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (board,))
        inst = _instruction(conn, board)
        assert kb.route_owner_instruction(conn, inst.id) == "routed"
        task = kb.get_task(conn, board)
        assert task is not None
        assert task.status == "ready"


def test_unassigned_instruction_is_durable_but_not_claimed_as_routed(board):
    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET assignee=NULL,status='blocked' WHERE id=?", (board,))
        inst = _instruction(conn, board, "unassigned")
        assert kb.route_owner_instruction(conn, inst.id) == "unroutable"
        stored = kb.get_owner_instruction(conn, inst.id)
        task = kb.get_task(conn, board)
        assert stored is not None
        assert task is not None
        assert stored.status == "unroutable"
        assert task.status == "blocked"


def test_dispatcher_migrates_legacy_pending_instruction_to_normal_card(board, monkeypatch):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda profile: True)
    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (board,))
        inst = _instruction(conn, board)
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda *args, **kwargs: spawned.append(args) or 123)
        stored = kb.get_owner_instruction(conn, inst.id)
        assert stored is not None
        assert stored.status == "routed"
        assert len(spawned) == 1
