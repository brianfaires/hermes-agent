"""Durable Discord conversation events and explicit ``!log`` exports.

Transport and Kanban writes remain outside this module.  An export is frozen
before its side effect so retries use the same event set and byte-identical
payload even when newer Discord messages arrive.
"""
from __future__ import annotations

import hashlib
import json
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
    author_id: str | None = None
    discord_message_link: str | None = None
    reply_context: str | None = None
    binding_task_id: str | None = None
    binding_interval: str | None = None
    metadata_json: str = "{}"


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
    author_id: str | None = None,
    discord_message_link: str | None = None,
    reply_context: str | None = None,
    binding_task_id: str | None = None,
    binding_interval: str | None = None,
    attachments: Sequence[dict] = (),
    artifacts: Sequence[dict] = (),
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
    metadata_json = json.dumps(
        {"attachments": list(attachments), "artifacts": list(artifacts)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    if binding_key and (binding_task_id is None or binding_interval is None):
        epoch = conn.execute(
            "SELECT task_id,started_at,ended_at FROM mirror_binding_epochs WHERE binding_key=?",
            (binding_key,),
        ).fetchone()
        if epoch is not None:
            binding_task_id = binding_task_id or str(epoch[0])
            binding_interval = binding_interval or f"{epoch[1]}..{epoch[2] if epoch[2] is not None else 'open'}"
    conn.execute(
        """
        INSERT OR IGNORE INTO mirror_conversation_events (
          discord_message_id, thread_id, binding_key, event_class,
          author_label, content, replied_to_message_id, discord_created_at,
          author_id, discord_message_link, reply_context, binding_task_id,
          binding_interval, metadata_json, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            str(author_id) if author_id else None,
            str(discord_message_link) if discord_message_link else None,
            str(reply_context) if reply_context else None,
            str(binding_task_id) if binding_task_id else None,
            str(binding_interval) if binding_interval else None,
            metadata_json,
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
        author_id=row["author_id"],
        discord_message_link=row["discord_message_link"],
        reply_context=row["reply_context"],
        binding_task_id=row["binding_task_id"],
        binding_interval=row["binding_interval"],
        metadata_json=str(row["metadata_json"] or "{}"),
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
    """Render a deterministic, self-contained source-attributed transcript."""
    if not events:
        raise ValueError("at least one conversation event is required")
    lines = ["[Discord discussion log v1]", ""]
    for event in events:
        stamp = str(event.discord_created_at) if event.discord_created_at is not None else "timestamp unavailable"
        author = event.author_label
        if event.author_id:
            author += f" (Discord user {event.author_id})"
        lines.extend([f"{author}:", event.content, f"Event: {event.event_class}; timestamp: {stamp}"])
        if event.replied_to_message_id:
            context = f" — {event.reply_context}" if event.reply_context else ""
            lines.append(f"↳ reply to {event.replied_to_message_id}{context}")
        if event.discord_message_link:
            lines.append(f"Discord: {event.discord_message_link}")
        destination = event.binding_task_id or event.binding_key
        if destination:
            interval = f"; interval {event.binding_interval}" if event.binding_interval else ""
            lines.append(f"Binding: {event.binding_key or 'legacy'} → card {destination}{interval}")
        metadata = json.loads(event.metadata_json or "{}")
        for attachment in metadata.get("attachments", []):
            lines.append("Attachment: " + _render_reference(attachment))
        for artifact in metadata.get("artifacts", []):
            lines.append("Artifact: " + _render_reference(artifact))
        lines.append("")
    note = str(note or "").strip()
    if note:
        lines.extend(["Log note:", note, ""])
    lines.append("Source messages: " + ", ".join(event.discord_message_id for event in events))
    return "\n".join(lines).strip()


def _render_reference(value: object) -> str:
    if not isinstance(value, dict):
        return str(value)
    return ", ".join(f"{key}={value[key]}" for key in sorted(value) if value[key] is not None)


# Leave room for the idempotency marker appended by the transport.
KANBAN_COMMENT_LIMIT = 15_500


def split_log_comment(payload: str, *, limit: int = KANBAN_COMMENT_LIMIT) -> tuple[str, ...]:
    """Deterministically split UTF-8 text, preferring transcript boundaries."""
    if limit < 128:
        raise ValueError("comment limit is too small")
    chunks: list[str] = []
    rest = payload
    while len(rest.encode("utf-8")) > limit:
        candidate = rest.encode("utf-8")[:limit]
        while True:
            try:
                text = candidate.decode("utf-8")
                break
            except UnicodeDecodeError:
                candidate = candidate[:-1]
        cut = text.rfind("\n\n")
        if cut < limit // 3:
            cut = len(text)
        chunks.append(text[:cut])
        rest = rest[len(text[:cut]):]
    if rest:
        chunks.append(rest)
    return tuple(chunks)


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
            _ensure_delivery_chunks(conn, existing)
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
        chunks = split_log_comment(payload)
        conn.executemany(
            """INSERT INTO mirror_conversation_delivery_chunks
               (operation_id,chunk_index,chunk_count,payload,payload_hash,status)
               VALUES (?,?,?,?,?,'pending')""",
            [
                (operation_id, index, len(chunks), chunk,
                 hashlib.sha256(chunk.encode("utf-8")).hexdigest())
                for index, chunk in enumerate(chunks)
            ],
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


def _ensure_delivery_chunks(conn: sqlite3.Connection, delivery: sqlite3.Row) -> None:
    """Backfill byte-stable chunks for deliveries created before chunk support."""
    operation_id = str(delivery["operation_id"])
    if conn.execute(
        "SELECT 1 FROM mirror_conversation_delivery_chunks WHERE operation_id=? LIMIT 1",
        (operation_id,),
    ).fetchone() is not None:
        return
    chunks = split_log_comment(str(delivery["payload"]))
    conn.executemany(
        """INSERT INTO mirror_conversation_delivery_chunks
           (operation_id,chunk_index,chunk_count,payload,payload_hash,status)
           VALUES (?,?,?,?,?,'pending')""",
        [(operation_id, index, len(chunks), chunk,
          hashlib.sha256(chunk.encode("utf-8")).hexdigest())
         for index, chunk in enumerate(chunks)],
    )


def claim_log_chunks(
    conn: sqlite3.Connection, *, worker_id: str, now: int | None = None,
    lease_seconds: int = 60, limit: int = 20,
) -> list[sqlite3.Row]:
    """Lease due frozen chunks; safe for a reusable supervised runner."""
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        raise ValueError("worker_id is required")
    now = int(time.time()) if now is None else int(now)
    conn.execute("BEGIN IMMEDIATE")
    rows = conn.execute(
        """SELECT c.*,d.task_id,d.trigger_discord_message_id
           FROM mirror_conversation_delivery_chunks c
           JOIN mirror_conversation_deliveries d USING(operation_id)
           WHERE c.status!='delivered' AND (c.next_attempt_at IS NULL OR c.next_attempt_at<=?)
             AND (c.lease_expires_at IS NULL OR c.lease_expires_at<=?)
           ORDER BY d.created_at,c.operation_id,c.chunk_index LIMIT ?""",
        (now, now, int(limit)),
    ).fetchall()
    for row in rows:
        conn.execute(
            """UPDATE mirror_conversation_delivery_chunks SET lease_owner=?,lease_expires_at=?
               WHERE operation_id=? AND chunk_index=?""",
            (worker_id, now + lease_seconds, row["operation_id"], row["chunk_index"]),
        )
    conn.commit()
    return rows


def mark_log_chunk(
    conn: sqlite3.Connection, *, operation_id: str, chunk_index: int,
    worker_id: str, comment_id: int | None = None, error: str | None = None,
    now: int | None = None,
) -> None:
    """Confirm a chunk or durably back it off; uncertain writes are failures."""
    now = int(time.time()) if now is None else int(now)
    if comment_id is not None:
        cursor = conn.execute(
            """UPDATE mirror_conversation_delivery_chunks
               SET status='delivered',attempt_count=attempt_count+1,kanban_comment_id=?,
                   delivered_at=?,last_error=NULL,lease_owner=NULL,lease_expires_at=NULL
               WHERE operation_id=? AND chunk_index=? AND lease_owner=? AND status!='delivered'""",
            (comment_id, now, operation_id, chunk_index, worker_id),
        )
    else:
        cursor = conn.execute(
            """UPDATE mirror_conversation_delivery_chunks
               SET status='failed',attempt_count=attempt_count+1,last_error=?,next_attempt_at=?,
                   lease_owner=NULL,lease_expires_at=NULL
               WHERE operation_id=? AND chunk_index=? AND lease_owner=? AND status!='delivered'""",
            (error or "unconfirmed Kanban write", now + 2 ** min(10, _chunk_attempt(conn, operation_id, chunk_index)),
             operation_id, chunk_index, worker_id),
        )
    if cursor.rowcount != 1:
        conn.rollback()
        raise RuntimeError("log chunk lease lost")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM mirror_conversation_delivery_chunks WHERE operation_id=? AND status!='delivered'",
        (operation_id,),
    ).fetchone()[0]
    if not remaining:
        ids = conn.execute(
            "SELECT kanban_comment_id FROM mirror_conversation_delivery_chunks WHERE operation_id=? ORDER BY chunk_index",
            (operation_id,),
        ).fetchall()
        mark_log_delivery(conn, operation_id=operation_id, status="delivered", kanban_comment_id=int(ids[-1][0]))
    else:
        conn.commit()


def _chunk_attempt(conn: sqlite3.Connection, operation_id: str, chunk_index: int) -> int:
    return int(conn.execute(
        "SELECT attempt_count FROM mirror_conversation_delivery_chunks WHERE operation_id=? AND chunk_index=?",
        (operation_id, chunk_index),
    ).fetchone()[0]) + 1


def recover_log_deliveries(
    mirror_conn: sqlite3.Connection, *, worker_id: str, write_comment,
    now: int | None = None, limit: int = 20,
) -> dict[str, int]:
    """Retry frozen chunks via an idempotent ``write_comment`` callback."""
    stats = {"claimed": 0, "delivered": 0, "failed": 0}
    for row in claim_log_chunks(mirror_conn, worker_id=worker_id, now=now, limit=limit):
        stats["claimed"] += 1
        marker = f"[discord-log-operation:{row['operation_id']}:{row['chunk_index'] + 1}/{row['chunk_count']}]"
        try:
            comment_id = write_comment(str(row["task_id"]), str(row["payload"]), marker)
            if comment_id is None:
                raise RuntimeError("Kanban write outcome was not confirmed")
            mark_log_chunk(mirror_conn, operation_id=str(row["operation_id"]),
                           chunk_index=int(row["chunk_index"]), worker_id=worker_id,
                           comment_id=int(comment_id), now=now)
            stats["delivered"] += 1
        except Exception as exc:
            mark_log_chunk(mirror_conn, operation_id=str(row["operation_id"]),
                           chunk_index=int(row["chunk_index"]), worker_id=worker_id,
                           error=str(exc), now=now)
            stats["failed"] += 1
    return stats


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
