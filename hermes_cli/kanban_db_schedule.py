"""Atomic scheduling and in-place parked-reason refreshes."""
from __future__ import annotations

import json
import sqlite3


def schedule_task(
    conn: sqlite3.Connection, task_id: str, *, reason: str | None = None,
    expected_run_id: int | None = None, comment_author: str | None = None,
    ci_wait: dict | None = None,
) -> bool:
    """Refreshes preserve the parked attempt; identical reasons are no-ops."""
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        binding = None
        if ci_wait is not None:
            from hermes_cli.kanban_db_ci import park_binding
            binding = park_binding(conn, task_id, expected_run_id, ci_wait)
        task = kb.get_task(conn, task_id)
        if task is None or (expected_run_id is not None
                            and task.current_run_id != int(expected_run_id)):
            return False
        if task.status == "scheduled":
            # Refreshing a reason must never reclaim a conflicting live claim.
            if any((task.current_run_id, task.claim_lock, task.claim_expires, task.worker_pid)):
                return False
            parked = conn.execute(
                "SELECT run_id, payload FROM task_events WHERE task_id = ? AND kind = 'scheduled' "
                "ORDER BY id DESC LIMIT 1", (task_id,),
            ).fetchone()
            if parked and json.loads(parked["payload"] or "{}").get("reason") == reason:
                return True
            run_id = parked["run_id"] if parked else None
        else:
            if task.status not in ("todo", "ready", "running", "blocked"):
                return False
            conn.execute(
                "UPDATE tasks SET status = 'scheduled', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL WHERE id = ?", (task_id,),
            )
            run_id = kb._end_or_synthesize_run(
                conn, task_id, outcome="scheduled", status="scheduled", summary=reason,
                synthesize=bool(reason),
            )
        payload: dict[str, object] = {"reason": reason}
        if binding is not None:
            payload["ci_wait"] = binding
        kb._append_event(conn, task_id, "scheduled", payload, run_id=run_id)
        if reason and comment_author is not None:
            kb.add_comment(conn, task_id, comment_author, f"SCHEDULED: {reason}")
        return True
