from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb



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
