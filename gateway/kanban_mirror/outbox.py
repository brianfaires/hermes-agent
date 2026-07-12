"""Durable Discord outbox for routed Kanban conversation responses.

The frozen envelope is inserted before any adapter call.  Retries read the
stored JSON rather than rebuilding it from mutable routing or agent state.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

OUTBOX_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mirror_discord_outbox (
  operation_id TEXT PRIMARY KEY,
  correlation_id TEXT NOT NULL,
  target_profile TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  reply_to_message_id TEXT,
  payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  discord_message_id TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  delivered_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mirror_discord_outbox_pending
ON mirror_discord_outbox(status, created_at);
"""

_CLAIM_LEASE_SECONDS = 300


def ensure_outbox_schema(conn: sqlite3.Connection) -> None:
    """Create the outbox and add recovery columns to older databases."""
    conn.executescript(OUTBOX_SCHEMA_SQL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(mirror_discord_outbox)")}
    for name, declaration in {
        "next_attempt_at": "INTEGER", "lease_owner": "TEXT",
        "lease_expires_at": "INTEGER", "confirmation_needed_at": "INTEGER",
        "quarantined_at": "INTEGER",
    }.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE mirror_discord_outbox ADD COLUMN {name} {declaration}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mirror_discord_outbox_due ON mirror_discord_outbox(status,next_attempt_at,created_at)")


@dataclass(frozen=True)
class OutboundEnvelope:
    profile: str
    thread_id: str
    reply_to_message_id: str | None
    content: str
    attachments: tuple[str, ...]
    correlation_id: str

    def frozen_json(self) -> str:
        return json.dumps(
            {
                "attachments": list(self.attachments),
                "content": self.content,
                "correlation_id": self.correlation_id,
                "profile": self.profile,
                "reply_to_message_id": self.reply_to_message_id,
                "thread_id": self.thread_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


def operation_id_for(envelope: OutboundEnvelope) -> str:
    """Stable per routed turn/profile; payload changes cannot create a new send."""
    raw = f"{envelope.correlation_id}\0{envelope.profile}".encode("utf-8")
    return "discord-response:" + hashlib.sha256(raw).hexdigest()


def enqueue(conn: sqlite3.Connection, envelope: OutboundEnvelope) -> str:
    if not all((envelope.profile.strip(), envelope.thread_id.strip(), envelope.correlation_id.strip())):
        raise ValueError("profile, thread_id, and correlation_id are required")
    payload = envelope.frozen_json()
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    operation_id = operation_id_for(envelope)
    now = int(time.time())
    ensure_outbox_schema(conn)
    conn.execute(
        """INSERT OR IGNORE INTO mirror_discord_outbox
           (operation_id, correlation_id, target_profile, thread_id,
            reply_to_message_id, payload, payload_hash, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (operation_id, envelope.correlation_id, envelope.profile, envelope.thread_id,
         envelope.reply_to_message_id, payload, payload_hash, now, now),
    )
    row = conn.execute(
        "SELECT payload_hash FROM mirror_discord_outbox WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if row["payload_hash"] != payload_hash:
        conn.rollback()
        raise ValueError("outbox operation already exists with a different frozen payload")
    conn.commit()
    return operation_id


def get(conn: sqlite3.Connection, operation_id: str) -> sqlite3.Row | None:
    ensure_outbox_schema(conn)
    return conn.execute(
        "SELECT * FROM mirror_discord_outbox WHERE operation_id=?", (operation_id,)
    ).fetchone()


async def deliver(
    conn: sqlite3.Connection,
    operation_id: str,
    adapter: Any | None,
    *,
    send: Callable[[Any, dict[str, Any]], Awaitable[Any]],
) -> bool:
    """Attempt one frozen delivery; unavailable/failed adapters remain pending."""
    row = get(conn, operation_id)
    if row is None:
        raise KeyError(operation_id)
    if row["status"] == "delivered":
        return True
    now = int(time.time())
    if adapter is None or not bool(getattr(adapter, "is_connected", False)):
        error = "target profile adapter is missing or disconnected"
        conn.execute(
            "UPDATE mirror_discord_outbox SET attempt_count=attempt_count+1,last_error=?,updated_at=? WHERE operation_id=?",
            (error, now, operation_id),
        )
        conn.commit()
        return False
    # Claim before the external side effect. A crashed claim becomes retryable
    # after a bounded lease; a live second worker cannot double-send.
    claimed = conn.execute(
        """UPDATE mirror_discord_outbox SET status='sending',updated_at=?
           WHERE operation_id=? AND (
             status='pending' OR (status='sending' AND updated_at < ?)
           )""",
        (now, operation_id, now - _CLAIM_LEASE_SECONDS),
    ).rowcount
    conn.commit()
    if claimed != 1:
        return False
    payload = json.loads(row["payload"])
    try:
        result = await send(adapter, payload)
        success = bool(getattr(result, "success", False))
        message_id = getattr(result, "message_id", None)
        if not success or not message_id:
            raise RuntimeError(getattr(result, "error", None) or "Discord send was not confirmed")
    except Exception as exc:
        conn.execute(
            """UPDATE mirror_discord_outbox SET status='pending',attempt_count=attempt_count+1,
               last_error=?,updated_at=? WHERE operation_id=? AND status='sending'""",
            (str(exc), now, operation_id),
        )
        conn.commit()
        return False
    conn.execute(
        """UPDATE mirror_discord_outbox SET status='delivered',attempt_count=attempt_count+1,
           last_error=NULL,discord_message_id=?,updated_at=?,delivered_at=?
           WHERE operation_id=? AND status='sending'""",
        (str(message_id), now, now, operation_id),
    )
    conn.commit()
    return True
