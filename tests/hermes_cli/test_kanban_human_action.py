"""Human Action Kanban task behavior."""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban as kc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_migration_adds_human_action_state_to_legacy_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = home / "kanban.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    with kb.connect(db_path) as migrated:
        task_columns = {r["name"] for r in migrated.execute("PRAGMA table_info(tasks)")}
        tables = {
            r["name"]
            for r in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    assert "task_kind" in task_columns
    assert "human_actions" in tables


def test_human_action_crud_and_brian_queue_filter(kanban_home):
    with kb.connect() as conn:
        engineering = kb.create_task(conn, title="Ship candidate", assignee="alice")
        human = kb.create_human_action(
            conn,
            title="Verify staged build",
            linked_task_id=engineering,
            candidate_repo="nous/hermes-agent",
            candidate_sha="abc123",
            candidate_build="build-42",
            candidate_environment="staging",
            instructions=["Open staging", "Run smoke test"],
            expected_result="Smoke test succeeds",
            expires_at=int(time.time()) + 3600,
        )
        task = kb.get_task(conn, human)
        action = kb.get_human_action(conn, human)
        queue = kb.list_brian_queue(conn)

    assert task.task_kind == "Human Action"
    assert task.assignee == "Brian"
    assert task.status == "blocked"
    assert action["owner"] == "Brian"
    assert action["linked_task_id"] == engineering
    assert action["instructions"] == ["Open staging", "Run smoke test"]
    assert [item["task"].id for item in queue] == [human]


def test_human_action_creation_has_no_dispatchable_intermediate_state(
    kanban_home, monkeypatch
):
    original_create_task = kb.create_task
    seen: list[tuple[str, str]] = []

    def recording_create_task(*args, **kwargs):
        task_id = original_create_task(*args, **kwargs)
        conn = args[0]
        task = kb.get_task(conn, task_id)
        seen.append((task.status, task.task_kind))
        return task_id

    monkeypatch.setattr(kb, "create_task", recording_create_task)

    with kb.connect() as conn:
        kb.create_human_action(
            conn,
            title="Manual approval",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="sha",
            instructions=["Approve"],
            expected_result="Approved",
        )

    assert seen == [("blocked", "Routine")]


def test_brian_queue_contains_only_open_human_actions(kanban_home):
    now = int(time.time())
    with kb.connect() as conn:
        fresh_passed = kb.create_human_action(
            conn,
            title="Fresh pass",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="fresh",
            instructions=["Check"],
            expected_result="Passes",
            expires_at=now + 3600,
        )
        kb.resolve_human_action(
            conn,
            fresh_passed,
            candidate_repo="repo",
            candidate_sha="fresh",
            outcome="Passed",
            evidence="ok",
        )

        failed = kb.create_human_action(
            conn,
            title="Failed",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="failed",
            instructions=["Check"],
            expected_result="Passes",
        )
        kb.resolve_human_action(
            conn,
            failed,
            candidate_repo="repo",
            candidate_sha="failed",
            outcome="Failed",
            evidence="failed",
        )

        expired_passed = kb.create_human_action(
            conn,
            title="Expired pass",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="expired",
            instructions=["Check"],
            expected_result="Passes",
            expires_at=now - 1,
        )
        kb.resolve_human_action(
            conn,
            expired_passed,
            candidate_repo="repo",
            candidate_sha="expired",
            outcome="Passed",
            evidence="stale pass",
        )

        superseded = kb.create_human_action(
            conn,
            title="Superseded",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="old",
            instructions=["Check"],
            expected_result="Passes",
        )
        kb.resolve_human_action(
            conn,
            superseded,
            candidate_repo="repo",
            candidate_sha="old",
            outcome="Passed",
            evidence="old pass",
        )
        kb.supersede_human_action(
            conn,
            superseded,
            candidate_repo="repo",
            candidate_sha="new",
        )

        assert kb.get_task(conn, fresh_passed).status == "done"
        assert kb.get_task(conn, expired_passed).status == "blocked"
        queue_ids = [item["task"].id for item in kb.list_brian_queue(conn)]

    assert fresh_passed not in queue_ids
    assert failed in queue_ids
    assert expired_passed in queue_ids
    assert superseded in queue_ids


def test_stale_passed_human_action_reopens_and_demotes_children(kanban_home):
    with kb.connect() as conn:
        human = kb.create_human_action(
            conn,
            title="Aging approval",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="sha",
            instructions=["Check"],
            expected_result="Passes",
            expires_at=int(time.time()) + 3600,
        )
        child = kb.create_task(conn, title="Dependent", assignee="alice", parents=[human])
        kb.resolve_human_action(
            conn,
            human,
            candidate_repo="repo",
            candidate_sha="sha",
            outcome="Passed",
            evidence="fresh",
        )
        assert kb.get_task(conn, human).status == "done"
        assert kb.get_task(conn, child).status == "ready"

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE human_actions SET expires_at = ? WHERE task_id = ?",
                (int(time.time()) - 1, human),
            )

        queue_ids = [item["task"].id for item in kb.list_brian_queue(conn)]

        assert queue_ids == [human]
        assert kb.get_task(conn, human).status == "blocked"
        assert kb.get_task(conn, child).status == "todo"


def test_human_action_is_never_claimed_or_dispatched(kanban_home, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists",
        lambda profile: profile.casefold() == "brian",
        raising=False,
    )
    spawned: list[str] = []
    with kb.connect() as conn:
        human = kb.create_human_action(
            conn,
            title="Manual approval",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="sha",
            instructions=["Approve"],
            expected_result="Approved",
        )
        assert kb.claim_task(conn, human) is None
        assert kb.has_spawnable_ready(conn) is False
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id) or 123,
        )
        task = kb.get_task(conn, human)

    assert spawned == []
    assert result.spawned == []
    assert human not in result.skipped_nonspawnable
    assert task.status == "blocked"
    assert task.claim_lock is None


def test_regular_task_assigned_to_brian_stays_dispatchable(kanban_home, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists",
        lambda profile: profile.casefold() == "brian",
        raising=False,
    )
    spawned: list[str] = []
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Agent Brian work", assignee="Brian")
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append(task.id) or 123,
        )

    assert spawned == [task_id]
    assert result.spawned[0][0] == task_id


def test_archived_human_action_resolve_and_supersede_are_immutable(kanban_home):
    with kb.connect() as conn:
        engineering = kb.create_task(conn, title="Engineering work", assignee="alice")
        human = kb.create_human_action(
            conn,
            title="Manual approval",
            linked_task_id=engineering,
            candidate_repo="repo",
            candidate_sha="sha1",
            instructions=["Approve"],
            expected_result="Approved",
        )
        assert kb.get_task(conn, engineering).status == "todo"

        assert kb.archive_task(conn, human)
        before_task = kb.get_task(conn, human)
        before_child = kb.get_task(conn, engineering)
        before_action = kb.get_human_action(conn, human)
        before_events = [
            dict(row)
            for row in conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
                (human,),
            )
        ]

        resolved = kb.resolve_human_action(
            conn,
            human,
            candidate_repo="repo",
            candidate_sha="sha1",
            outcome="Passed",
            evidence="archived evidence",
        )
        superseded = kb.supersede_human_action(
            conn,
            human,
            candidate_repo="repo",
            candidate_sha="sha2",
            instructions=["Approve the new candidate"],
            expected_result="Approved again",
        )

        after_events = [
            dict(row)
            for row in conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
                (human,),
            )
        ]

        assert resolved is False
        assert superseded is False
        assert kb.get_task(conn, human).status == before_task.status == "archived"
        assert kb.get_task(conn, engineering).status == before_child.status == "todo"
        assert kb.get_human_action(conn, human) == before_action
        assert after_events == before_events


def test_resolve_and_supersede_revalidate_archive_inside_transaction(
    kanban_home,
    monkeypatch,
):
    with kb.connect() as conn:
        resolve_id = kb.create_human_action(
            conn,
            title="Resolve race",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="sha-resolve",
            instructions=["Approve"],
            expected_result="Approved",
        )
        supersede_id = kb.create_human_action(
            conn,
            title="Supersede race",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="sha-supersede",
            instructions=["Approve"],
            expected_result="Approved",
        )
        original_write_txn = kb.write_txn
        active_task = {"id": resolve_id}

        @contextmanager
        def archive_before_write(connection):
            connection.execute(
                "UPDATE tasks SET status = 'archived' WHERE id = ?",
                (active_task["id"],),
            )
            with original_write_txn(connection):
                yield

        monkeypatch.setattr(kb, "write_txn", archive_before_write)

        assert not kb.resolve_human_action(
            conn,
            resolve_id,
            candidate_repo="repo",
            candidate_sha="sha-resolve",
            outcome="Passed",
            evidence="too late",
        )
        resolve_task = kb.get_task(conn, resolve_id)
        resolve_action = kb.get_human_action(conn, resolve_id)
        assert resolve_task is not None and resolve_task.status == "archived"
        assert resolve_action is not None and resolve_action["outcome"] is None

        active_task["id"] = supersede_id
        assert not kb.supersede_human_action(
            conn,
            supersede_id,
            candidate_repo="repo",
            candidate_sha="new-sha",
        )
        supersede_task = kb.get_task(conn, supersede_id)
        supersede_action = kb.get_human_action(conn, supersede_id)
        assert supersede_task is not None and supersede_task.status == "archived"
        assert supersede_action is not None
        assert supersede_action["candidate_sha"] == "sha-supersede"


def _corrupt_running_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    claim_expires: int | None,
    worker_pid: int | None,
    started_at: int | None = None,
    max_runtime_seconds: int | None = None,
) -> None:
    host_lock = f"{kb._claimer_id().split(':', 1)[0]}:corrupt"
    with kb.write_txn(conn):
        conn.execute(
            """
            UPDATE tasks
               SET status = 'running',
                   claim_lock = ?,
                   claim_expires = ?,
                   worker_pid = ?,
                   started_at = ?,
                   max_runtime_seconds = ?,
                   last_heartbeat_at = ?
             WHERE id = ?
            """,
            (
                host_lock,
                claim_expires,
                worker_pid,
                started_at,
                max_runtime_seconds,
                (started_at - 7200) if started_at is not None else None,
                task_id,
            ),
        )


def test_human_action_automatic_recovery_paths_ignore_corrupt_running_rows(
    kanban_home,
    monkeypatch,
):
    now = int(time.time())
    calls: list[tuple[int, int]] = []

    def signal_hook(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        raise ProcessLookupError

    with kb.connect() as conn:
        stale_claim = kb.create_human_action(
            conn,
            title="Stale manual approval",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="sha",
            instructions=["Approve"],
            expected_result="Approved",
        )
        timed_out = kb.create_human_action(
            conn,
            title="Timed out manual approval",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="sha",
            instructions=["Approve"],
            expected_result="Approved",
        )
        crashed = kb.create_human_action(
            conn,
            title="Crashed manual approval",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="sha",
            instructions=["Approve"],
            expected_result="Approved",
        )
        heartbeat_stale = kb.create_human_action(
            conn,
            title="Heartbeat-stale manual approval",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="sha",
            instructions=["Approve"],
            expected_result="Approved",
        )

        _corrupt_running_task(
            conn,
            stale_claim,
            claim_expires=now - 60,
            worker_pid=os.getpid(),
            started_at=now - 7200,
        )
        _corrupt_running_task(
            conn,
            timed_out,
            claim_expires=now + 3600,
            worker_pid=os.getpid(),
            started_at=now - 7200,
            max_runtime_seconds=1,
        )
        _corrupt_running_task(
            conn,
            crashed,
            claim_expires=now + 3600,
            worker_pid=999999,
            started_at=now - 7200,
        )
        _corrupt_running_task(
            conn,
            heartbeat_stale,
            claim_expires=now + 3600,
            worker_pid=os.getpid(),
            started_at=now - 7200,
        )

        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

        assert kb.release_stale_claims(conn, signal_fn=signal_hook) == 0
        assert kb.enforce_max_runtime(conn, signal_fn=signal_hook) == []
        assert kb.detect_crashed_workers(conn) == []
        assert kb.detect_stale_running(
            conn,
            stale_timeout_seconds=1,
            signal_fn=signal_hook,
        ) == []

        assert calls == []
        for task_id in (stale_claim, timed_out, crashed, heartbeat_stale):
            task = kb.get_task(conn, task_id)
            assert task.status == "running"
            assert task.claim_lock is not None
            assert task.worker_pid is not None

        ordinary_stale = kb.create_task(conn, title="Ordinary stale", assignee="alice")
        ordinary_timeout = kb.create_task(conn, title="Ordinary timeout", assignee="alice")
        ordinary_crashed = kb.create_task(conn, title="Ordinary crashed", assignee="alice")
        ordinary_heartbeat_stale = kb.create_task(
            conn,
            title="Ordinary heartbeat stale",
            assignee="alice",
        )
        _corrupt_running_task(
            conn,
            ordinary_stale,
            claim_expires=now - 60,
            worker_pid=os.getpid(),
            started_at=now - 7200,
        )
        _corrupt_running_task(
            conn,
            ordinary_timeout,
            claim_expires=now + 3600,
            worker_pid=os.getpid(),
            started_at=now - 7200,
            max_runtime_seconds=1,
        )
        _corrupt_running_task(
            conn,
            ordinary_crashed,
            claim_expires=now + 3600,
            worker_pid=999998,
            started_at=now - 7200,
        )
        _corrupt_running_task(
            conn,
            ordinary_heartbeat_stale,
            claim_expires=now + 3600,
            worker_pid=None,
            started_at=now - 7200,
        )

        assert kb.release_stale_claims(conn, signal_fn=signal_hook) == 1
        assert kb.enforce_max_runtime(conn, signal_fn=signal_hook) == [ordinary_timeout]
        assert kb.detect_crashed_workers(conn) == [ordinary_crashed]
        assert kb.detect_stale_running(
            conn,
            stale_timeout_seconds=1,
            signal_fn=signal_hook,
        ) == [ordinary_heartbeat_stale]
        assert [
            task_id
            for task_id in (
                ordinary_stale,
                ordinary_timeout,
                ordinary_crashed,
                ordinary_heartbeat_stale,
            )
            if kb.get_task(conn, task_id).status == "ready"
        ] == [
            ordinary_stale,
            ordinary_timeout,
            ordinary_crashed,
            ordinary_heartbeat_stale,
        ]
        assert len(calls) == 2


def test_human_action_generic_recovery_paths_fail_closed_even_if_state_is_corrupt(
    kanban_home,
):
    with kb.connect() as conn:
        human = kb.create_human_action(
            conn,
            title="Manual approval",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="sha",
            instructions=["Approve"],
            expected_result="Approved",
        )
        with kb.write_txn(conn):
            conn.execute(
                """
                UPDATE tasks
                   SET status = 'running',
                       claim_lock = 'manual-corrupt',
                       claim_expires = ?,
                       worker_pid = NULL
                 WHERE id = ?
                """,
                (int(time.time()) + 3600, human),
            )
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
            (human,),
        ).fetchone()[0]
        assert kb.heartbeat_claim(
            conn,
            human,
            claimer="manual-corrupt",
        ) is False
        assert kb.heartbeat_worker(conn, human, note="generic heartbeat") is False
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
            (human,),
        ).fetchone()[0] == event_count
        assert kb.reclaim_task(conn, human, reason="generic recovery") is False
        assert kb._record_task_failure(
            conn,
            human,
            error="generic failure accounting",
            outcome="spawn_failed",
            failure_limit=3,
            release_claim=True,
            end_run=False,
        ) is False
        task = kb.get_task(conn, human)
        assert task.status == "running"
        assert task.claim_lock == "manual-corrupt"

        with kb.write_txn(conn):
            conn.execute(
                """
                UPDATE tasks
                   SET status = 'ready',
                       claim_lock = NULL,
                       block_kind = ?,
                       block_recurrences = ?
                 WHERE id = ?
                """,
                ("needs_input", kb.BLOCK_RECURRENCE_LIMIT - 1, human),
            )
        assert kb.block_task(conn, human, reason="still blocked", kind="needs_input") is False
        assert kb.get_task(conn, human).status == "ready"

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (human,))
        assert kb.specify_triage_task(conn, human, body="generic spec") is False
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert kb.decompose_triage_task(
            conn,
            human,
            root_assignee="alice",
            children=[{"title": "Should not exist", "assignee": "alice"}],
        ) is None
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == task_count
        assert kb.get_task(conn, human).status == "triage"


def test_task_kind_create_and_list_filter_are_backward_compatible(kanban_home):
    with kb.connect() as conn:
        routine = kb.create_task(conn, title="Routine task", assignee="alice")
        critical = kb.create_task(
            conn,
            title="Critical task",
            assignee="alice",
            task_kind="Critical",
        )
        assert kb.get_task(conn, routine).task_kind == "Routine"
        assert [t.id for t in kb.list_tasks(conn, task_kind="Critical")] == [critical]


def test_generic_create_refuses_human_action_kind(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="create_human_action"):
            kb.create_task(
                conn,
                title="Malformed human action",
                task_kind="Human Action",
            )


def test_generic_lifecycle_cannot_take_over_human_action(kanban_home):
    with kb.connect() as conn:
        human = kb.create_human_action(
            conn,
            title="Manual approval",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="sha",
            instructions=["Approve"],
            expected_result="Approved",
        )

        assert not kb.assign_task(conn, human, "alice")
        assert kb.get_task(conn, human).assignee == "Brian"
        assert not kb.complete_task(conn, human, result="generic done")
        assert not kb.block_task(conn, human, reason="generic block")
        assert not kb.schedule_task(conn, human)
        assert not kb.unblock_task(conn, human)

        assert kb.archive_task(conn, human)
        archived = kb.get_task(conn, human)
        assert archived.status == "archived"
        assert archived.assignee == "Brian"


def test_owner_instruction_cannot_reroute_human_action(kanban_home):
    with kb.connect() as conn:
        engineering = kb.create_task(conn, title="Engineering work", assignee="alice")
        human = kb.create_human_action(
            conn,
            title="Manual approval",
            linked_task_id=engineering,
            candidate_repo="repo",
            candidate_sha="sha",
            instructions=["Approve"],
            expected_result="Approved",
        )
        assert kb.resolve_human_action(
            conn,
            human,
            candidate_repo="repo",
            candidate_sha="sha",
            outcome="Passed",
            evidence="approved",
        )
        human_task = kb.get_task(conn, human)
        engineering_task = kb.get_task(conn, engineering)
        assert human_task is not None and human_task.status == "done"
        assert engineering_task is not None and engineering_task.status == "ready"

        rerun = kb.create_owner_instruction(
            conn,
            task_id=human,
            assignee="Brian",
            source="test",
            source_key="human-action-rerun",
            actor="Brian",
            body="Instruction: rerun_request",
        )
        assert kb.route_owner_instruction(
            conn,
            rerun.id,
            explicit_rerun=True,
        ) == "ignored"
        human_task = kb.get_task(conn, human)
        engineering_task = kb.get_task(conn, engineering)
        assert human_task is not None and human_task.status == "done"
        assert engineering_task is not None and engineering_task.status == "ready"

        assert kb.archive_task(conn, human)
        archived_rerun = kb.create_owner_instruction(
            conn,
            task_id=human,
            assignee="Brian",
            source="test",
            source_key="archived-human-action-rerun",
            actor="Brian",
            body="Instruction: rerun_request",
        )
        assert kb.route_owner_instruction(
            conn,
            archived_rerun.id,
            explicit_rerun=True,
        ) == "ignored"
        human_task = kb.get_task(conn, human)
        engineering_task = kb.get_task(conn, engineering)
        assert human_task is not None and human_task.status == "archived"
        assert engineering_task is not None and engineering_task.status == "ready"


def test_owner_instruction_rerun_respects_expired_human_parent(kanban_home):
    with kb.connect() as conn:
        engineering = kb.create_task(conn, title="Engineering work", assignee="alice")
        human = kb.create_human_action(
            conn,
            title="Manual approval",
            linked_task_id=engineering,
            candidate_repo="repo",
            candidate_sha="sha",
            instructions=["Approve"],
            expected_result="Approved",
        )
        assert kb.resolve_human_action(
            conn,
            human,
            candidate_repo="repo",
            candidate_sha="sha",
            outcome="Passed",
            evidence="approved",
            expires_at=int(time.time()) + 3600,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE human_actions SET expires_at = ? WHERE task_id = ?",
                (int(time.time()) - 1, human),
            )
            conn.execute(
                "UPDATE tasks SET status = 'blocked' WHERE id = ?",
                (engineering,),
            )

        rerun = kb.create_owner_instruction(
            conn,
            task_id=engineering,
            assignee="alice",
            source="test",
            source_key="expired-human-parent-rerun",
            actor="Brian",
            body="Instruction: rerun_request",
        )
        assert kb.route_owner_instruction(
            conn,
            rerun.id,
            explicit_rerun=True,
        ) == "routed"
        engineering_task = kb.get_task(conn, engineering)
        assert engineering_task is not None and engineering_task.status == "todo"
        assert not kb.human_action_satisfies_dependency(conn, human)


@pytest.mark.parametrize("outcome", ["Failed", "Blocked", "Needs clarification"])
def test_only_passed_human_action_satisfies_dependency(kanban_home, outcome):
    with kb.connect() as conn:
        human = kb.create_human_action(
            conn,
            title="Check candidate",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="sha",
            instructions=["Check"],
            expected_result="Passed only",
        )
        child = kb.create_task(conn, title="Dependent", assignee="alice", parents=[human])
        assert kb.get_task(conn, child).status == "todo"
        kb.resolve_human_action(
            conn,
            human,
            candidate_repo="repo",
            candidate_sha="sha",
            outcome=outcome,
            evidence="evidence",
        )
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "todo"

        kb.resolve_human_action(
            conn,
            human,
            candidate_repo="repo",
            candidate_sha="sha",
            outcome="Passed",
            evidence="new evidence",
        )
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_human_action_candidate_mismatch_expiry_and_supersession_fail_closed(kanban_home):
    with kb.connect() as conn:
        human = kb.create_human_action(
            conn,
            title="Check exact candidate",
            linked_task_id=None,
            candidate_repo="repo",
            candidate_sha="sha1",
            instructions=["Check"],
            expected_result="Passed only",
            expires_at=int(time.time()) + 3600,
        )
        child = kb.create_task(conn, title="Dependent", assignee="alice", parents=[human])
        with pytest.raises(ValueError, match="candidate mismatch"):
            kb.resolve_human_action(
                conn,
                human,
                candidate_repo="repo",
                candidate_sha="sha2",
                outcome="Passed",
                evidence="wrong candidate",
            )

        kb.resolve_human_action(
            conn,
            human,
            candidate_repo="repo",
            candidate_sha="sha1",
            outcome="Passed",
            evidence="ok",
        )
        assert kb.human_action_satisfies_dependency(conn, human)

        kb.supersede_human_action(
            conn,
            human,
            candidate_repo="repo",
            candidate_sha="sha2",
            instructions=["Check new candidate"],
            expected_result="New candidate passes",
        )
        assert not kb.human_action_satisfies_dependency(conn, human)
        assert kb.get_task(conn, child).status == "todo"

        kb.resolve_human_action(
            conn,
            human,
            candidate_repo="repo",
            candidate_sha="sha2",
            outcome="Passed",
            evidence="ok",
            expires_at=int(time.time()) - 1,
        )
        assert not kb.human_action_satisfies_dependency(conn, human)
        assert kb.get_task(conn, human).status == "blocked"
        assert [item["task"].id for item in kb.list_brian_queue(conn)] == [human]
        assert kb.claim_task(conn, child) is None


def test_cli_creates_lists_and_resolves_human_action(kanban_home, capsys):
    created = kc.run_slash(
        "human-action create 'Manual smoke' --repo repo --sha sha "
        "--instruction 'Open staging' --instruction 'Confirm login' "
        "--expected 'Login works' --json"
    )
    task_id = __import__("json").loads(created)["id"]

    queue = __import__("json").loads(kc.run_slash("brian-queue --json"))
    assert queue[0]["task"]["id"] == task_id

    shown = __import__("json").loads(kc.run_slash(f"show {task_id} --json"))
    assert shown["human_action"]["candidate_repo"] == "repo"

    resolved = kc.run_slash(
        f"human-action resolve {task_id} --repo repo --sha sha "
        "--outcome Passed --evidence 'staging evidence'"
    )
    assert "Resolved" in resolved
