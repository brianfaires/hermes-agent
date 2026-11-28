"""Native time/CI parking; public entry point is kanban_db.schedule_task."""
from __future__ import annotations

import sqlite3
from typing import Any, Optional


def schedule_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    ci_wait: Optional[dict] = None,
) -> bool:
    """Park in ``scheduled`` (waiting on time, not a human; not dispatchable)
    until ``unblock_task`` re-gates it."""
    from hermes_cli import kanban_db as _kb
    with _kb.write_txn(conn):
        binding = None
        if ci_wait is not None:
            from hermes_cli.kanban_db_ci import park_binding
            binding = park_binding(conn, task_id, expected_run_id, ci_wait)
        params: list[Any] = [task_id]
        sql = """
            UPDATE tasks
               SET status       = 'scheduled',
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL
             WHERE id = ?
               AND status IN ('todo', 'ready', 'running', 'blocked')
        """
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params.append(int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return False
        run_id = _kb._end_or_synthesize_run(
            conn, task_id, outcome="scheduled", status="scheduled", summary=reason, synthesize=bool(reason),
        )
        payload = {"reason": reason}
        if binding is not None:
            payload["ci_wait"] = binding
        _kb._append_event(conn, task_id, "scheduled", payload, run_id=run_id)
        return True
