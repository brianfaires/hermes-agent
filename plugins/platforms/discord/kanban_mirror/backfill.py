"""Crash-safe, per-thread Discord history ingestion.

The fetch and processing interfaces are deliberately transport-free so this
foundation can be exercised with fixture SQLite and fake Discord history.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, Sequence

from plugins.platforms.discord.kanban_mirror.state import active_thread_binding, is_thread_quarantined


@dataclass(frozen=True)
class DiscordInbound:
    message_id: str
    thread_id: str
    content: str | None
    author_label: str = "unknown"
    created_at: int | None = None
    replied_to_message_id: str | None = None
    relevant: bool = True
    forum_channel_id: str | None = None
    author_id: str | None = None
    mentioned_user_ids: tuple[str, ...] = ()
    replied_to_author_id: str | None = None
    replied_to_author_is_bot: bool = False
    # Frozen by the configured ingress adapter; replay must not reinterpret
    # authorization using whichever multiplexed adapter later claims the row.
    authorized: bool = True
    authorization_reason: str = "open_policy"
    authorization_policy: dict | None = None


@dataclass(frozen=True)
class HistoryPage:
    messages: Sequence[DiscordInbound]
    has_more: bool = False


class HistoryFetcher(Protocol):
    async def fetch_after(self, thread_id: str, after: str | None, limit: int) -> HistoryPage: ...


@dataclass(frozen=True)
class BackfillResult:
    ingested: int
    duplicates: int
    pages: int
    limited: bool
    cursor: str | None


Processor = Callable[[DiscordInbound, str], None | Awaitable[None]]


class DiscordBackfillIngestor:
    """Serialize one thread while permitting independent thread progress."""

    def __init__(self, conn: sqlite3.Connection, *, page_size: int = 100,
                 max_pages: int = 10, max_age_seconds: int = 7 * 86400,
                 processor: Processor | None = None, clock: Callable[[], int] | None = None):
        if page_size < 1 or max_pages < 1 or max_age_seconds < 0:
            raise ValueError("backfill bounds must be non-negative and page bounds positive")
        self.conn, self.page_size, self.max_pages = conn, page_size, max_pages
        self.max_age_seconds, self.processor = max_age_seconds, processor
        self.clock = clock or (lambda: int(time.time()))
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, thread_id: str) -> asyncio.Lock:
        return self._locks.setdefault(str(thread_id), asyncio.Lock())

    def cursor(self, thread_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT last_message_id FROM mirror_discord_thread_cursors WHERE thread_id=?", (str(thread_id),)
        ).fetchone()
        return None if row is None else row[0]

    async def ingest_live(self, message: DiscordInbound) -> BackfillResult:
        async with self._lock(message.thread_id):
            inserted = await self._persist(message, "live")
            return BackfillResult(int(inserted), int(not inserted), 0, False, self.cursor(message.thread_id))

    async def backfill(self, thread_id: str, fetcher: HistoryFetcher) -> BackfillResult:
        thread_id = str(thread_id).strip()
        if not thread_id:
            raise ValueError("thread_id is required")
        async with self._lock(thread_id):
            ingested = duplicates = pages = 0
            limited = False
            for _ in range(self.max_pages):
                after = self.cursor(thread_id)
                page = await fetcher.fetch_after(thread_id, after, self.page_size)
                pages += 1
                messages = sorted(page.messages, key=lambda m: _snowflake(m.message_id))
                age_stopped = False
                for message in messages:
                    if message.thread_id != thread_id:
                        raise ValueError("history fetcher returned a message for another thread")
                    if message.created_at is not None and message.created_at < self.clock() - self.max_age_seconds:
                        limited = True
                        age_stopped = True
                        break
                    inserted = await self._persist(message, "backfill")
                    ingested += int(inserted)
                    duplicates += int(not inserted)
                if age_stopped or not page.has_more:
                    break
            else:
                limited = True
            self.conn.execute(
                "UPDATE mirror_discord_thread_cursors SET backlog_limited=? WHERE thread_id=?",
                (int(limited), thread_id),
            )
            self.conn.commit()
            return BackfillResult(ingested, duplicates, pages, limited, self.cursor(thread_id))

    async def _persist(self, message: DiscordInbound, via: str) -> bool:
        message_id, thread_id = str(message.message_id).strip(), str(message.thread_id).strip()
        if not message_id or not thread_id:
            raise ValueError("message_id and thread_id are required")
        now = self.clock()
        malformed = not isinstance(message.content, str) or not message.content
        event_class = "system.error" if malformed else "mirror.ack" if not message.relevant else "conversation.human"
        content = message.content if isinstance(message.content, str) and message.content else "[malformed Discord message]"
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            quarantined = is_thread_quarantined(self.conn, thread_id)
            binding = active_thread_binding(self.conn, thread_id)
            prior_event = self.conn.execute(
                "SELECT id,thread_id FROM mirror_conversation_events WHERE discord_message_id=?", (message_id,)
            ).fetchone()
            classification = (
                "already_recorded" if prior_event is not None else
                "malformed" if malformed else "noise" if not message.relevant else
                "quarantined" if quarantined else "unmapped" if binding is None else "pending"
            )
            # Unroutable human input is not acknowledged: reconciliation may
            # make a quarantined or missing/ambiguous binding routable later.
            status = "pending" if classification in {"pending", "quarantined", "unmapped"} else "processed"
            existing = self.conn.execute(
                "SELECT 1 FROM mirror_discord_inbound_state WHERE discord_message_id=?", (message_id,)
            ).fetchone()
            if existing is None:
                self.conn.execute("""INSERT OR IGNORE INTO mirror_conversation_events
                    (discord_message_id,thread_id,binding_key,event_class,author_label,content,
                     replied_to_message_id,discord_created_at,recorded_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (message_id, thread_id, binding.binding_key if binding else None, event_class,
                     message.author_label or "unknown", content, message.replied_to_message_id,
                     message.created_at, now))
                event_row = self.conn.execute(
                    "SELECT id,thread_id FROM mirror_conversation_events WHERE discord_message_id=?", (message_id,)
                ).fetchone()
                if event_row is None or str(event_row["thread_id"]) != thread_id:
                    raise ValueError("Discord message identity belongs to another thread")
                event_id = event_row["id"]
                payload = json.dumps({
                    "message_id": message_id, "thread_id": thread_id, "content": content,
                    "author_label": message.author_label or "unknown", "author_id": message.author_id,
                    "created_at": message.created_at, "replied_to_message_id": message.replied_to_message_id,
                    "forum_channel_id": message.forum_channel_id,
                    "mentioned_user_ids": list(message.mentioned_user_ids),
                    "replied_to_author_id": message.replied_to_author_id,
                    "replied_to_author_is_bot": message.replied_to_author_is_bot,
                    "authorized": message.authorized,
                    "authorization_reason": message.authorization_reason,
                    "authorization_policy": message.authorization_policy or {},
                }, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                self.conn.execute("""INSERT INTO mirror_discord_inbound_state
                    (discord_message_id,thread_id,conversation_event_id,classification,processing_status,
                     observed_via,observed_at,processed_at,payload) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (message_id, thread_id, event_id, classification, status, via, now,
                     now if status == "processed" else None, payload))
            current = self.cursor(thread_id)
            if current is None or _snowflake(message_id) > _snowflake(current):
                self.conn.execute("""INSERT INTO mirror_discord_thread_cursors
                    (thread_id,last_message_id,last_message_created_at,observed_at,backlog_limited)
                    VALUES (?,?,?,?,0) ON CONFLICT(thread_id) DO UPDATE SET
                    last_message_id=excluded.last_message_id,
                    last_message_created_at=excluded.last_message_created_at,
                    observed_at=excluded.observed_at""",
                    (thread_id, message_id, message.created_at, now))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        if existing is None and classification == "pending" and self.processor is not None:
            try:
                result = self.processor(message, classification)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # The durable pending row is the retry authority. This callback
                # is only a best-effort wake-up for a later processing loop.
                pass
        return existing is None

    def mark_processed(self, discord_message_id: str) -> None:
        """Acknowledge later handler completion without changing observation state."""
        cursor = self.conn.execute("""UPDATE mirror_discord_inbound_state
            SET processing_status='processed',processed_at=?
            WHERE discord_message_id=? AND processing_status='pending'""",
            (self.clock(), str(discord_message_id))).rowcount
        if not cursor:
            row = self.conn.execute(
                "SELECT 1 FROM mirror_discord_inbound_state WHERE discord_message_id=?",
                (str(discord_message_id),),
            ).fetchone()
            if row is None:
                raise KeyError(discord_message_id)
        self.conn.commit()

    def metrics(self, thread_id: str, *, latest_message_id: str | None = None) -> dict[str, int | str | None]:
        cursor = self.cursor(thread_id)
        oldest = self.conn.execute("""SELECT MIN(observed_at) FROM mirror_discord_inbound_state
            WHERE thread_id=? AND processing_status='pending'""", (str(thread_id),)).fetchone()[0]
        lag = None
        if latest_message_id is not None:
            lag = max(0, _snowflake(latest_message_id) - _snowflake(cursor or "0"))
        row = self.conn.execute(
            "SELECT backlog_limited FROM mirror_discord_thread_cursors WHERE thread_id=?", (str(thread_id),)
        ).fetchone()
        return {"cursor": cursor, "message_id_lag": lag, "oldest_unprocessed_at": oldest,
                "backlog_limited": 0 if row is None else int(row[0])}


def _snowflake(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Discord message ID: {value!r}") from exc