"""Deterministic local-state repairs discovered by complete reconciliation scans.

These helpers never call Discord or Kanban. They only converge state when the
existing evidence identifies exactly one safe result; ambiguity remains
quarantined for an operator.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import Mapping

from .reconciliation import resolve_thread_quarantine
from .state import is_terminal


def _timestamp(value: object) -> int | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def resolve_recoverable_quarantines(
    conn: sqlite3.Connection,
    *,
    observed_thread_ids: set[str],
    cards: set[str],
    now: int | None = None,
) -> list[str]:
    """Acknowledge clean latches only when the live mapping is unambiguous.

    ``reconcile_mirror_state`` intentionally leaves quarantine latched. A
    complete live observation may acknowledge it automatically only when one
    open epoch maps the observed thread to a current board card, the initiative
    membership agrees, and no transition is still in flight.
    """
    stamp = int(time.time()) if now is None else int(now)
    observed = {str(thread_id) for thread_id in observed_thread_ids}
    known_cards = {str(task_id) for task_id in cards}
    resolved: list[str] = []
    rows = conn.execute(
        "SELECT thread_id FROM mirror_thread_quarantine WHERE resolved_at IS NULL ORDER BY thread_id"
    ).fetchall()
    for row in rows:
        thread_id = str(row[0])
        if thread_id not in observed:
            continue
        mappings = conn.execute(
            """SELECT b.task_id
               FROM mirror_binding_epochs b
               JOIN mirror_initiatives i ON i.thread_id=b.thread_id AND i.kind='post'
               JOIN mirror_members m ON m.initiative_id=i.id AND m.task_id=b.task_id
               WHERE b.thread_id=? AND b.state='open'""",
            (thread_id,),
        ).fetchall()
        if len(mappings) != 1 or str(mappings[0][0]) not in known_cards:
            continue
        initiative_count = conn.execute(
            "SELECT COUNT(*) FROM mirror_initiatives WHERE kind='post' AND thread_id=?",
            (thread_id,),
        ).fetchone()[0]
        if int(initiative_count) != 1:
            continue
        if conn.execute(
            "SELECT 1 FROM mirror_binding_transitions WHERE thread_id=? AND state!='starter_verified' LIMIT 1",
            (thread_id,),
        ).fetchone():
            continue
        if resolve_thread_quarantine(conn, thread_id, now=stamp):
            resolved.append(thread_id)
    return resolved


def recover_pending_inbound_bindings(
    conn: sqlite3.Connection,
    *,
    cards: Mapping[str, tuple[str, object | None]],
    now: int | None = None,
    limit: int = 500,
) -> dict[str, int]:
    """Recover NULL event bindings from one exact historical epoch.

    Events at or before an already-terminal card's completion are preserved but
    dispositioned instead of replayed. Other events are rebound and woken for
    normal durable processing. Missing timestamps, overlapping/no epochs,
    active quarantine, in-flight transitions, and terminal cards without a
    completion timestamp all fail closed.
    """
    stamp = int(time.time()) if now is None else int(now)
    if limit < 1:
        raise ValueError("limit must be positive")
    card_state = {str(task_id): (str(status), completed_at)
                  for task_id, (status, completed_at) in cards.items()}
    counts = {"rebound": 0, "superseded": 0, "deduplicated": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            """SELECT s.discord_message_id,s.thread_id,e.discord_created_at
               FROM mirror_discord_inbound_state s
               JOIN mirror_conversation_events e ON e.id=s.conversation_event_id
               WHERE s.processing_status='pending' AND e.binding_key IS NULL
                 AND (s.lease_expires_at IS NULL OR s.lease_expires_at<=?)
                 AND NOT EXISTS (
                   SELECT 1 FROM mirror_thread_quarantine q
                   WHERE q.thread_id=s.thread_id AND q.resolved_at IS NULL)
               ORDER BY s.observed_at,s.conversation_event_id LIMIT ?""",
            (stamp, int(limit)),
        ).fetchall()
        for row in rows:
            message_id, thread_id = str(row[0]), str(row[1])
            event_at = _timestamp(row[2])
            if event_at is None:
                continue
            if conn.execute(
                "SELECT 1 FROM mirror_binding_transitions WHERE thread_id=? AND state!='starter_verified' LIMIT 1",
                (thread_id,),
            ).fetchone():
                continue
            epochs = conn.execute(
                """SELECT binding_key,task_id,started_at,ended_at
                   FROM mirror_binding_epochs
                   WHERE thread_id=? AND started_at<=?
                     AND (ended_at IS NULL OR ?<ended_at)""",
                (thread_id, event_at, event_at),
            ).fetchall()
            if len(epochs) != 1:
                continue
            binding_key, task_id = str(epochs[0][0]), str(epochs[0][1])
            if task_id not in card_state:
                continue
            status, completed_raw = card_state[task_id]
            completed_at = _timestamp(completed_raw)
            terminal = is_terminal(status)
            receipt = conn.execute(
                "SELECT task_id FROM mirror_inbox_receipts WHERE discord_message_id=?",
                (message_id,),
            ).fetchone()
            receipt_matches = receipt is not None and str(receipt[0]) == task_id
            if terminal and completed_at is None and not receipt_matches:
                continue
            interval = f"{epochs[0][2]}..{epochs[0][3] if epochs[0][3] is not None else 'open'}"
            changed = conn.execute(
                """UPDATE mirror_conversation_events
                   SET binding_key=?,binding_task_id=?,binding_interval=?
                   WHERE discord_message_id=? AND binding_key IS NULL""",
                (binding_key, task_id, interval, message_id),
            ).rowcount
            if changed != 1:
                continue
            if receipt_matches:
                disposition = "already_recorded_legacy"
                detail = "existing inbox receipt confirms prior Kanban write"
                counts["deduplicated"] += 1
            elif terminal and completed_at is not None and event_at <= completed_at:
                disposition = "superseded_by_terminal_completion"
                detail = "historical input predates terminal completion"
                counts["superseded"] += 1
            else:
                conn.execute(
                    """UPDATE mirror_discord_inbound_state
                       SET next_attempt_at=?,lease_expires_at=NULL,last_error=NULL
                       WHERE discord_message_id=? AND processing_status='pending'""",
                    (stamp, message_id),
                )
                counts["rebound"] += 1
                continue
            conn.execute(
                """INSERT OR IGNORE INTO mirror_discord_inbound_dispositions
                   (discord_message_id,correlation_id,disposition,detail,created_at)
                   VALUES (?,NULL,?,?,?)""",
                (message_id, disposition, detail, stamp),
            )
            conn.execute(
                """UPDATE mirror_discord_inbound_state
                   SET processing_status='processed',processed_at=?,next_attempt_at=NULL,
                       lease_expires_at=NULL,last_error=NULL
                   WHERE discord_message_id=? AND processing_status='pending'""",
                (stamp, message_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return counts
