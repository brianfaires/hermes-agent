"""Explicit post-PR continuation never manufactures other release/claim rights."""
from pathlib import Path
import json

import pytest

from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets: [kanban]\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    kb.init_db()
    with kb.connect() as conn:
        yield conn, monkeypatch


def pr_task(conn, monkeypatch):
    tid = kb.create_task(conn, title="continue qualified release", assignee="default")
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    kb.add_comment(conn, tid, "worker", "Review https://github.com/example/repo/pull/42")
    monkeypatch.setattr(kb.time, "time", lambda: 2001)
    return tid


def test_orchestrator_explicit_resume_overrides_only_pr_guard(board):
    conn, monkeypatch = board
    tid = pr_task(conn, monkeypatch)
    assert kb.check_respawn_guard(conn, tid) == "active_pr"
    out = json.loads(kt._handle_unblock({
        "task_id": tid,
        "resume_after_pr": True,
        "reason": "Brian authorized release continuation in coordination message 123",
    }))
    assert out.get("ok") is True, out
    assert kb.check_respawn_guard(conn, tid) is None
    assert kb.get_task(conn, tid).status == "ready"


def test_host_cli_explicit_resume_records_provenance(board):
    from argparse import Namespace
    from hermes_cli import kanban

    conn, monkeypatch = board
    tid = pr_task(conn, monkeypatch)
    assert kanban._cmd_unblock(Namespace(
        task_ids=[tid], reason="Brian approved continuation", resume_after_pr=True,
    )) == 0
    assert kb.check_respawn_guard(conn, tid) is None
    event = kb.list_events(conn, tid)[-1]
    assert event.kind == "pr_resume_authorized"
    assert event.payload["source"] == "host_cli"


def resume(conn, tid):
    return kb.authorize_pr_resume(
        conn, tid, actor="default", source="orchestrator_tool",
        reason="Brian approved release continuation",
    )


@pytest.mark.parametrize("kind", [
    "status", "promoted", "unblocked", "reclaimed", "crashed", "spawn_failed",
    "promoted_manual", "timed_out", "rate_limited",
])
def test_ordinary_and_automatic_events_do_not_grant_consent(board, kind):
    conn, monkeypatch = board
    tid = pr_task(conn, monkeypatch)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, kind, {"actor": "Brian", "reason": "resume"})
    kb.add_comment(conn, tid, "Brian", "resume approved pr_resume_authorized")
    assert kb.check_respawn_guard(conn, tid) == "active_pr"


def test_same_second_causal_resume_and_new_pr_invalidate_old_consent(board):
    conn, monkeypatch = board
    tid = pr_task(conn, monkeypatch)
    monkeypatch.setattr(kb.time, "time", lambda: 2000)
    assert resume(conn, tid)
    assert kb.check_respawn_guard(conn, tid) is None
    kb.add_comment(conn, tid, "worker", "Another https://github.com/example/repo/pull/43")
    assert kb.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("reason", [None, "", "   ", 123])
def test_missing_or_invalid_reason_has_no_mutation(board, reason):
    conn, monkeypatch = board
    tid = pr_task(conn, monkeypatch)
    before = kb.list_events(conn, tid)
    out = json.loads(kt._handle_unblock({
        "task_id": tid, "resume_after_pr": True, "reason": reason,
    }))
    assert "error" in out
    assert kb.list_events(conn, tid) == before
    assert kb.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("context", ["worker", "child", "cron", "unconfigured", "anonymous"])
def test_untrusted_context_cannot_self_authorize(board, context):
    from contextlib import nullcontext
    from agent.delegation_context import delegated_child_context, non_dispatcher_owned_context

    conn, monkeypatch = board
    tid = pr_task(conn, monkeypatch)
    scope = nullcontext()
    if context == "worker":
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    elif context == "child":
        scope = delegated_child_context()
    elif context == "cron":
        scope = non_dispatcher_owned_context()
    elif context == "unconfigured":
        monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: False)
    else:
        monkeypatch.delenv("HERMES_PROFILE")
    before = kb.list_events(conn, tid)
    with scope:
        out = json.loads(kt._handle_unblock({
            "task_id": tid, "resume_after_pr": True, "reason": "Approved",
            "actor": "Brian", "source": "host_cli",
        }))
    assert "error" in out
    assert kb.list_events(conn, tid) == before


def test_resume_preserves_auth_quota_and_recent_success(board):
    conn, monkeypatch = board
    tid = pr_task(conn, monkeypatch)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET last_failure_error = 'authentication failed' WHERE id = ?", (tid,))
    assert resume(conn, tid)
    assert kb.check_respawn_guard(conn, tid) == "blocker_auth"
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET last_failure_error = NULL WHERE id = ?", (tid,))
    assert kb.claim_task(conn, tid)
    assert kb.complete_task(conn, tid, summary="qualified")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    assert kb.check_respawn_guard(conn, tid) == "recent_success"
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET outcome = 'rate_limited' WHERE task_id = ?", (tid,))
    assert kb.check_respawn_guard(conn, tid) == "rate_limit_cooldown"


def test_hold_and_live_owner_cannot_be_released_by_pr_consent(board):
    conn, monkeypatch = board
    tid = pr_task(conn, monkeypatch)
    assert kb.block_task(conn, tid, reason="human hold", kind="needs_input")
    assert not resume(conn, tid)
    assert kb.has_active_control_hold(conn, tid)
    assert kb.get_task(conn, tid).status == "blocked"
    assert kb.unblock_task(conn, tid)
    assert resume(conn, tid)
    assert kb.claim_task(conn, tid)
    run = kb.get_task(conn, tid).current_run_id
    assert not resume(conn, tid)
    assert not kb.claim_task(conn, tid)
    assert kb.get_task(conn, tid).current_run_id == run


def test_pending_parent_and_concurrency_keep_dispatch_deferred(board):
    from hermes_cli import profiles

    conn, monkeypatch = board
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = pr_task(conn, monkeypatch)
    parent = kb.create_task(conn, title="unfinished parent")
    kb.link_tasks(conn, parent, tid)
    assert resume(conn, tid)
    before = kb.get_task(conn, tid).status
    spawned = []
    tick = kb.dispatch_once(conn, spawn_fn=lambda *args: spawned.append(args), reconcile_orphans=False)
    assert not spawned
    assert not tick.spawned
    assert before == kb.get_task(conn, tid).status == "todo"
    assert kb.complete_task(conn, parent, summary="parent done")
    other = kb.create_task(conn, title="active slot", assignee="default")
    assert kb.claim_task(conn, other)
    tick = kb.dispatch_once(conn, spawn_fn=lambda *args: spawned.append(args), max_in_progress=1, reconcile_orphans=False)
    assert not spawned
    assert not tick.spawned
    assert kb.get_task(conn, tid).status == "ready"


def test_dispatcher_real_claim_path_runs_authorized_continuation(board):
    from hermes_cli import profiles

    conn, monkeypatch = board
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = pr_task(conn, monkeypatch)
    seen = []
    tick = kb.dispatch_once(conn, spawn_fn=lambda *args: seen.append(args), reconcile_orphans=False)
    assert tick.respawn_guarded == [(tid, "active_pr")]
    assert not seen
    assert resume(conn, tid)
    tick = kb.dispatch_once(conn, spawn_fn=lambda *args: seen.append(args), reconcile_orphans=False)
    assert len(tick.spawned) == len(seen) == 1
    assert kb.get_task(conn, tid).status == "running"


@pytest.mark.parametrize("source", [[], {}, None, "worker", "comment"])
def test_malformed_or_untrusted_approval_event_is_guarded(board, source):
    conn, monkeypatch = board
    tid = pr_task(conn, monkeypatch)
    pr = kb.list_comments(conn, tid)[-1]
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "pr_resume_authorized", {
            "actor": "Brian", "source": source, "reason": "approved",
            "pr_comment_id": pr.id,
        })
    assert kb.check_respawn_guard(conn, tid) == "active_pr"
