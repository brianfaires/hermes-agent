"""A conversation's separately dispatched Kanban worker is visible to its goal judge."""
import hermes_bootstrap  # Settle the checkout before binding disposable state.

from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import goals, kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_db_dispatch import _set_worker_pid

pytestmark = pytest.mark.platforms('linux')

@pytest.mark.parametrize("same_owner", [True, False], ids=["owned-worker", "unrelated-worker"])
@pytest.mark.parametrize("board", ["default", "engineering"])
def test_goal_can_wait_for_its_worker_without_spending_the_turn_budget(tmp_path, monkeypatch, same_owner, board):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("{}\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    if board != "default":
        kb.create_board(board)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    goals._DB_CACHE.clear()
    manager = goals.GoalManager("coordinator")
    manager.set("Deliver the assigned change", max_turns=3)
    worker = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    seen = []

    def judge(_goal, _response, **kwargs):
        context = goals._render_background_block(kwargs.get("background_processes"))
        seen.append(context)
        if f"pid {worker.pid}" in context:
            return "wait", "Assigned worker is still running", False, {"pid": worker.pid}, False
        return "continue", "No running dependency is visible", False, None, False

    monkeypatch.setattr(goals, "judge_goal", judge)
    try:
        with kbc.connect_closing(board=board) as conn:
            task = kb.create_task(conn, title="Implement the assigned change", assignee="default",
                                  session_id="coordinator" if same_owner else "other-conversation",
                                  workspace_kind="dir", workspace_path=str(tmp_path))
            assert kb.claim_task(conn, task)
            _set_worker_pid(conn, task, worker.pid)
            run_id = kb.get_task(conn, task).current_run_id
            for _ in range(3):
                snapshot = goals.gather_background_processes(owner_task_id="coordinator")
                manager.evaluate_after_turn("Waiting for the assigned worker", background_processes=snapshot)
            if same_owner:
                assert manager.is_waiting(), "The goal spent its budget instead of seeing its running worker"
                assert manager.state.turns_used == 1
                assert len(seen) == 1
                assert f"{board}/{task} run {run_id}" in seen[0]
                worker.communicate(timeout=5)
                assert not manager.is_waiting()
                assert all(p.get("pid") != worker.pid for p in goals.gather_background_processes(owner_task_id="coordinator"))
            else:
                assert not manager.is_waiting(), "An unrelated worker must not park this goal"
                assert all(f"pid {worker.pid}" not in context for context in seen)
    finally:
        if worker.poll() is None:
            worker.communicate(timeout=5)
        goals._DB_CACHE.clear()


def test_worker_snapshot_does_not_initialize_boards_or_accept_recycled_pids(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    goals.gather_background_processes(owner_task_id="coordinator")
    assert not kb.kanban_db_path().exists()
    worker = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        with kbc.connect_closing() as conn:
            task = kb.create_task(conn, title="An old run", assignee="default", session_id="coordinator",
                                  workspace_kind="dir", workspace_path=str(tmp_path))
            assert kb.claim_task(conn, task)
            _set_worker_pid(conn, task, worker.pid)
            # A persisted row with the right PID but another process incarnation is stale.
            conn.execute("UPDATE tasks SET worker_started_at = ? WHERE id = ?", ("old-boot|0", task))
            assert all(p.get("pid") != worker.pid for p in goals.gather_background_processes(owner_task_id="coordinator"))
    finally:
        worker.communicate(timeout=5)
