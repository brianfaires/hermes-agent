"""Resolve blocking gate cards once the user approves."""
from __future__ import annotations

from hermes_cli import kanban_db as kb


def is_awaiting_user(conn, gate_id: str) -> bool:
    task = kb.get_task(conn, gate_id)
    return bool(task and task.status == "blocked")


def resolve_gate(conn, gate_id: str, *, approver: str, note: str = "") -> bool:
    task = kb.get_task(conn, gate_id)
    if task is None or task.status != "blocked":
        return False
    kb.unblock_task(conn, gate_id)
    summary = f"Gate approved by {approver}." + (f" {note}" if note else "")
    kb.complete_task(conn, gate_id, summary=summary,
                     metadata={"kind": "engteam_gate", "approver": approver})
    return True
