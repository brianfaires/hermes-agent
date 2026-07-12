"""Durable Discord conversation events and explicit ``!log`` exports.

Transport and Kanban writes remain outside this module.  An export is frozen
before its side effect so retries use the same event set and byte-identical
payload even when newer Discord messages arrive.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from typing import Literal, Sequence

EXPORTABLE_EVENT_CLASSES = frozenset(
    {
        "conversation.human",
        "conversation.agent",
        "directive.user",
        "directive.agent_disposition",
    }
)


@dataclass(frozen=True)
class LogCommand:
    mode: Literal["reply", "current", "all"]
    note: str = ""
    replied_to_message_id: str | None = None


@dataclass(frozen=True)
class ConversationEvent:
    id: int
    discord_message_id: str
    thread_id: str
    binding_key: str | None
    event_class: str
    author_label: str
    content: str
    replied_to_message_id: str | None
    discord_created_at: int | None


@dataclass(frozen=True)
class FrozenLogDelivery:
    operation_id: str
    trigger_discord_message_id: str
    thread_id: str
    task_id: str
    mode: str
    payload: str
    payload_hash: str
    status: str
    event_ids: tuple[int, ...]
    attempt_count: int
    kanban_comment_id: int | None


@dataclass(frozen=True)
class LogDeliveryTarget:
    """One binding-scoped Kanban destination for a log command."""

    binding_key: str | None
    task_id: str


def parse_log_command(text: str, *, replied_to_message_id: str | None = None) -> LogCommand | None:
    """Parse `!log`; unknown exclamation-prefixed text remains conversation."""
    body = (text or "").strip()
    if not body:
        return None
    parts = body.split(None, 1)
    if parts[0].lower() != "!log":
        return None
    argument = parts[1].strip() if len(parts) > 1 else ""
    if argument.lower() == "all":
        return LogCommand(mode="all")
    reply_id = str(replied_to_message_id or "").strip() or None
    if reply_id:
        return LogCommand(mode="reply", note=argument, replied_to_message_id=reply_id)
    return LogCommand(mode="current", note=argument)


def record_conversation_event(
    conn: sqlite3.Connection,
    *,
    discord_message_id: str,
    thread_id: str,
    binding_key: str | None,
    event_class: str,
    author_label: str,
    content: str,
    replied_to_message_id: str | None = None,
    discord_created_at: int | None = None,
    legacy_binding_key: str | None = None,
    commit: bool = True,
) -> ConversationEvent:
    """Insert once and return the original immutable event on replay."""
    message_id = str(discord_message_id or "").strip()
    thread_id = str(thread_id or "").strip()
    if not message_id or not thread_id:
        raise ValueError("discord_message_id and thread_id are required")
    if not str(content or ""):
        raise ValueError("content is required")
    if event_class not in EXPORTABLE_EVENT_CLASSES and not event_class.startswith(("mirror.", "system.")):
        raise ValueError(f"unsupported conversation event class: {event_class}")
    # Hold the mirror write lock across epoch resolution and insertion so a
    # concurrent transition cannot move the thread between those two steps.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    # Capture the epoch active at creation. Zero/ambiguous bindings remain NULL:
    # the event is preserved while current-card operations fail closed.
    if binding_key is None:
        from gateway.kanban_mirror.state import active_thread_binding

        binding = active_thread_binding(conn, thread_id)
        if binding is not None:
            binding_key = binding.binding_key
        elif legacy_binding_key is not None:
            epoch_count = conn.execute(
                "SELECT COUNT(*) FROM mirror_binding_epochs WHERE thread_id=?", (thread_id,)
            ).fetchone()[0]
            binding_key = str(legacy_binding_key) if not epoch_count else None
    conn.execute(
        """
        INSERT OR IGNORE INTO mirror_conversation_events (
          discord_message_id, thread_id, binding_key, event_class,
          author_label, content, replied_to_message_id,
          discord_created_at, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            thread_id,
            str(binding_key).strip() if binding_key is not None else None,
            event_class,
            str(author_label or "unknown").strip() or "unknown",
            str(content),
            str(replied_to_message_id).strip() if replied_to_message_id else None,
            int(discord_created_at) if discord_created_at is not None else None,
            int(time.time()),
        ),
    )
    if commit:
        conn.commit()
    row = conn.execute(
        "SELECT * FROM mirror_conversation_events WHERE discord_message_id = ?",
        (message_id,),
    ).fetchone()
    assert row is not None
    return _event_from_row(row)


def _event_from_row(row: sqlite3.Row) -> ConversationEvent:
    return ConversationEvent(
        id=int(row["id"]),
        discord_message_id=str(row["discord_message_id"]),
        thread_id=str(row["thread_id"]),
        binding_key=row["binding_key"],
        event_class=str(row["event_class"]),
        author_label=str(row["author_label"]),
        content=str(row["content"]),
        replied_to_message_id=row["replied_to_message_id"],
        discord_created_at=row["discord_created_at"],
    )


def select_log_events(
    conn: sqlite3.Connection,
    *,
    command: LogCommand,
    thread_id: str,
    binding_key: str | None,
) -> list[ConversationEvent]:
    """Select an export candidate set without advancing delivery state."""
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        raise ValueError("thread_id is required")
    if command.mode == "reply":
        row = conn.execute(
            "SELECT * FROM mirror_conversation_events WHERE thread_id = ? AND discord_message_id = ?",
            (thread_id, command.replied_to_message_id),
        ).fetchone()
        if row is None:
            return []
        event = _event_from_row(row)
        return [event] if event.event_class in EXPORTABLE_EVENT_CLASSES else []

    placeholders = ",".join("?" for _ in EXPORTABLE_EVENT_CLASSES)
    clauses = [
        "e.thread_id = ?",
        f"e.event_class IN ({placeholders})",
        # A batch event stays reserved by its first frozen operation even when
        # that operation is failed; retries must reuse the original operation
        # ID and payload rather than letting a second command duplicate it.
        # Reply-mode intentionally bypasses this reservation for explicit
        # repeat logging of one message.
        "NOT EXISTS ("
        "SELECT 1 FROM mirror_conversation_delivery_items i "
        "WHERE i.event_id = e.id"
        ")",
    ]
    params: list[object] = [thread_id, *sorted(EXPORTABLE_EVENT_CLASSES)]
    if command.mode == "current":
        if binding_key is None or not str(binding_key).strip():
            raise ValueError("current-binding log requires an unambiguous binding_key")
        clauses.append("e.binding_key = ?")
        params.append(str(binding_key).strip())
    rows = conn.execute(
        f"SELECT e.* FROM mirror_conversation_events e WHERE {' AND '.join(clauses)} "
        "ORDER BY COALESCE(e.discord_created_at, e.recorded_at), e.id",
        params,
    ).fetchall()
    return [_event_from_row(row) for row in rows]


def resolve_log_targets(
    conn: sqlite3.Connection, *, command: LogCommand, thread_id: str,
    legacy_task_id: str | None = None,
) -> list[LogDeliveryTarget]:
    """Resolve binding destinations without moving old history to the current card."""
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        raise ValueError("thread_id is required")
    epoch_count = conn.execute(
        "SELECT COUNT(*) FROM mirror_binding_epochs WHERE thread_id=?", (thread_id,)
    ).fetchone()[0]
    if epoch_count:
        from gateway.kanban_mirror.state import active_thread_binding

        active = active_thread_binding(conn, thread_id)
        if active is None:
            raise ValueError("thread does not have exactly one active binding")
        if command.mode == "current":
            return [LogDeliveryTarget(active.binding_key, active.task_id)]
        if command.mode == "reply":
            row = conn.execute(
                """SELECT b.binding_key,b.task_id FROM mirror_conversation_events e
                   JOIN mirror_binding_epochs b ON b.binding_key=e.binding_key
                   WHERE e.thread_id=? AND e.discord_message_id=?""",
                (thread_id, command.replied_to_message_id),
            ).fetchone()
            return [] if row is None else [LogDeliveryTarget(str(row[0]), str(row[1]))]
        rows = conn.execute(
            """SELECT DISTINCT b.binding_key,b.task_id,b.sequence
               FROM mirror_binding_epochs b JOIN mirror_conversation_events e ON e.binding_key=b.binding_key
               WHERE b.thread_id=? ORDER BY b.sequence""", (thread_id,),
        ).fetchall()
        return [LogDeliveryTarget(str(row[0]), str(row[1])) for row in rows]

    # Compatibility with the Phase-1 one-card mapping. The thread registry is
    # the sole authoritative destination when no epochs have been backfilled.
    legacy_task_id = str(legacy_task_id or "").strip()
    if not legacy_task_id:
        raise ValueError("legacy log requires the mapped task_id")
    if command.mode == "reply":
        row = conn.execute(
            "SELECT binding_key FROM mirror_conversation_events WHERE thread_id=? AND discord_message_id=?",
            (thread_id, command.replied_to_message_id),
        ).fetchone()
        keys = [row[0]] if row is not None else []
    else:
        # ``None`` deliberately means lifecycle-wide selection for legacy all.
        keys = [legacy_task_id if command.mode == "current" else None]
    if command.mode == "all":
        return [LogDeliveryTarget(None, legacy_task_id)]
    return [LogDeliveryTarget(str(key), legacy_task_id) for key in keys if key]


def render_log_comment(events: Sequence[ConversationEvent], *, note: str = "") -> str:
    """Render a deterministic source-attributed transcript."""
    if not events:
        raise ValueError("at least one conversation event is required")
    lines = ["[Discord discussion log v1]", ""]
    for event in events:
        lines.extend([f"{event.author_label}:", event.content, ""])
    note = str(note or "").strip()
    if note:
        lines.extend(["Log note:", note, ""])
    lines.append("Source messages: " + ", ".join(event.discord_message_id for event in events))
    return "\n".join(lines).strip()


def _delivery_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> FrozenLogDelivery:
    event_ids = tuple(
        int(item["event_id"])
        for item in conn.execute(
            "SELECT event_id FROM mirror_conversation_delivery_items WHERE operation_id = ? ORDER BY event_id",
            (row["operation_id"],),
        )
    )
    return FrozenLogDelivery(
        operation_id=str(row["operation_id"]),
        trigger_discord_message_id=str(row["trigger_discord_message_id"]),
        thread_id=str(row["thread_id"]),
        task_id=str(row["task_id"]),
        mode=str(row["mode"]),
        payload=str(row["payload"]),
        payload_hash=str(row["payload_hash"]),
        status=str(row["status"]),
        event_ids=event_ids,
        attempt_count=int(row["attempt_count"]),
        kanban_comment_id=row["kanban_comment_id"],
    )


def freeze_log_delivery(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    trigger_discord_message_id: str,
    thread_id: str,
    task_id: str,
    command: LogCommand,
    binding_key: str | None,
    scope_all_to_binding: bool = False,
) -> FrozenLogDelivery | None:
    """Atomically create or return a byte-stable pending delivery."""
    operation_id = str(operation_id or "").strip()
    trigger_id = str(trigger_discord_message_id or "").strip()
    thread_id = str(thread_id or "").strip()
    task_id = str(task_id or "").strip()
    if not operation_id or not trigger_id or not thread_id or not task_id:
        raise ValueError("operation_id, trigger_discord_message_id, thread_id, and task_id are required")

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM mirror_conversation_deliveries WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if existing is not None:
            supplied_identity = (trigger_id, str(thread_id), task_id, command.mode)
            frozen_identity = (
                str(existing["trigger_discord_message_id"]),
                str(existing["thread_id"]),
                str(existing["task_id"]),
                str(existing["mode"]),
            )
            if supplied_identity != frozen_identity:
                raise ValueError("operation_id already belongs to a different log request")
            conn.commit()
            return _delivery_from_row(conn, existing)
        selection_command = command
        if command.mode == "all" and scope_all_to_binding:
            selection_command = LogCommand(mode="current", note=command.note)
        events = select_log_events(
            conn,
            command=selection_command,
            thread_id=thread_id,
            binding_key=binding_key,
        )
        if not events:
            conn.commit()
            return None
        payload = render_log_comment(events, note=command.note)
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        now = int(time.time())
        conn.execute(
            """
            INSERT INTO mirror_conversation_deliveries (
              operation_id, trigger_discord_message_id, thread_id, task_id,
              mode, payload, payload_hash, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (operation_id, trigger_id, thread_id, task_id, command.mode, payload, payload_hash, now, now),
        )
        conn.executemany(
            "INSERT INTO mirror_conversation_delivery_items (operation_id, event_id) VALUES (?, ?)",
            [(operation_id, event.id) for event in events],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    row = conn.execute(
        "SELECT * FROM mirror_conversation_deliveries WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    assert row is not None
    return _delivery_from_row(conn, row)


def mark_log_delivery(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    status: Literal["delivered", "failed"],
    kanban_comment_id: int | None = None,
    error: str | None = None,
) -> FrozenLogDelivery:
    """Record one side-effect attempt without changing its frozen payload."""
    if status == "delivered" and kanban_comment_id is None:
        raise ValueError("delivered status requires kanban_comment_id")
    now = int(time.time())
    cursor = conn.execute(
        """
        UPDATE mirror_conversation_deliveries
        SET status = ?, attempt_count = attempt_count + 1,
            last_error = ?, kanban_comment_id = ?, updated_at = ?,
            delivered_at = CASE WHEN ? = 'delivered' THEN ? ELSE delivered_at END
        WHERE operation_id = ? AND status != 'delivered'
        """,
        (status, error, kanban_comment_id, now, status, now, operation_id),
    )
    if cursor.rowcount == 0:
        row = conn.execute(
            "SELECT * FROM mirror_conversation_deliveries WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(operation_id)
    conn.commit()
    row = conn.execute(
        "SELECT * FROM mirror_conversation_deliveries WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    assert row is not None
    return _delivery_from_row(conn, row)
