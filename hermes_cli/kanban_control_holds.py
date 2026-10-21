"""Durable operator consent gates shared by Kanban state transitions."""
from __future__ import annotations

import sqlite3


def has_active_control_hold(conn: sqlite3.Connection, task_id: str) -> bool:
    """Only an explicit release supersedes a human hold, including legacy creation."""
    from hermes_cli.kanban_db import _json_dict

    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind IN "
        "('created', 'blocked', 'block_loop_detected', 'gave_up', 'unblocked', "
        "'promoted_manual') ORDER BY id DESC", (task_id,),
    )
    for row in rows:
        kind = row['kind']
        payload = _json_dict(row['payload'])
        if kind in {'unblocked', 'promoted_manual'}:
            return False
        if kind in {'blocked', 'block_loop_detected'}:
            return True
        if kind == 'gave_up' and payload.get('sticky'):
            return True
        if kind == 'created' and payload.get('status') == 'blocked':
            return True
    # Older boards may have recurrence state without the corresponding events.
    row = conn.execute(
        'SELECT status, block_kind, block_recurrences FROM tasks WHERE id = ?', (task_id,),
    ).fetchone()
    return bool(row and row['status'] in {'blocked', 'triage'}
                and row['block_recurrences'] and row['block_kind'] != 'dependency')
