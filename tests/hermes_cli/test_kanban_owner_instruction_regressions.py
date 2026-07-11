from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

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


def test_accepted_instruction_counts_against_task_global_and_profile_caps(board, monkeypatch):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda profile: True)
    with kb.connect() as conn:
        inst = kb.claim_owner_instruction(conn, _instruction(conn, board, "same").id)
        kb.set_owner_instruction_pid(conn, inst.id, inst.claim_lock, os.getpid())
        second = kb.create_task(conn, title="task", assignee="same", triage=False)
        spawned = []
        result = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 123,
                                  max_in_progress=1, max_in_progress_per_profile=1)
        assert spawned == []
        assert kb.get_task(conn, second).status == "ready"
        assert result.spawned == []


def test_instruction_worker_cannot_create_cards(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", board)
    monkeypatch.setenv("HERMES_KANBAN_OWNER_INSTRUCTION", "42")
    with kb.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    result = json.loads(kanban_tools._handle_create({"title": "no", "assignee": "x"}))
    assert "instruction workers cannot create cards" in result["error"]
    with kb.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before


def test_spawn_uses_snapshotted_instruction_assignee_and_does_not_leak_task_claim(board, monkeypatch, tmp_path):
    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET claim_lock='original-task-token' WHERE id=?", (board,))
        task = kb.get_task(conn, board)
        inst = kb.claim_owner_instruction(conn, _instruction(conn, board, "snapshot-owner").id)
    captured = {}
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", lambda profile: str(tmp_path))
    monkeypatch.setattr(kb, "_kanban_worker_skill_available", lambda home: False)
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda home: None)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: captured.update(cmd=cmd, env=kw["env"]) or SimpleNamespace(pid=321))
    kb._default_spawn(task, str(tmp_path), owner_instruction=inst)
    assert captured["cmd"][captured["cmd"].index("-p") + 1] == "snapshot-owner"
    prompt = captured["cmd"][captured["cmd"].index("-q") + 1]
    assert "please inspect" in prompt
    assert f"owner instruction {inst.id}" in prompt
    assert captured["env"]["HERMES_PROFILE"] == "snapshot-owner"
    assert "HERMES_KANBAN_CLAIM_LOCK" not in captured["env"]
