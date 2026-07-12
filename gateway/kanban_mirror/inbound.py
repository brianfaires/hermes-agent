"""Leased durable processing for frozen mirrored Discord input."""
from __future__ import annotations
import asyncio, inspect, json, sqlite3, time
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

@dataclass(frozen=True)
class PendingInbound:
    message_id: str
    thread_id: str
    payload: dict
    attempt_count: int

@dataclass(frozen=True)
class ProcessResult:
    outcome: Literal["routed", "disposition", "retry"]
    correlation_id: str | None = None
    disposition: str | None = None
    detail: str | None = None

Handler = Callable[[PendingInbound], ProcessResult | Awaitable[ProcessResult]]

class PendingInboundRunner:
    """Claim only each thread's oldest row; independent threads progress concurrently."""
    def __init__(self, conn: sqlite3.Connection, handler: Handler, *, clock=None,
                 lease_seconds: int = 60, max_backoff: int = 300):
        self.conn, self.handler = conn, handler
        self.clock = clock or (lambda: int(time.time()))
        self.lease_seconds, self.max_backoff = lease_seconds, max_backoff

    def _claim(self) -> list[sqlite3.Row]:
        now = self.clock()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute("""SELECT s.* FROM mirror_discord_inbound_state s
              WHERE s.processing_status IN ('pending','leased','awaiting_response')
              AND (s.next_attempt_at IS NULL OR s.next_attempt_at<=?)
              AND (s.lease_expires_at IS NULL OR s.lease_expires_at<=?)
              AND NOT EXISTS (SELECT 1 FROM mirror_discord_inbound_state o
                WHERE o.thread_id=s.thread_id AND o.processing_status IN ('pending','leased','awaiting_response')
                AND (o.observed_at<s.observed_at OR (o.observed_at=s.observed_at AND o.conversation_event_id<s.conversation_event_id)))
              ORDER BY s.observed_at,s.conversation_event_id""", (now, now)).fetchall()
            for row in rows:
                self.conn.execute("UPDATE mirror_discord_inbound_state SET processing_status='leased',lease_expires_at=? WHERE discord_message_id=?",
                                  (now + self.lease_seconds, row["discord_message_id"]))
            self.conn.commit(); return rows
        except Exception:
            self.conn.rollback(); raise

    async def run_once(self) -> int:
        rows = self._claim()
        await asyncio.gather(*(self._process(row) for row in rows))
        return len(rows)

    async def _process(self, row: sqlite3.Row) -> None:
        mid, now = str(row["discord_message_id"]), self.clock()
        try:
            payload = json.loads(row["payload"] or "")
            if not isinstance(payload, dict) or str(payload.get("message_id")) != mid:
                raise ValueError("invalid frozen inbound payload")
        except Exception as exc:
            self._disposition(mid, "quarantined_malformed", str(exc), None, now); return
        correlation = row["correlation_id"]
        if correlation and self.conn.execute("SELECT 1 FROM mirror_discord_outbox WHERE correlation_id=?", (correlation,)).fetchone():
            self._complete(mid, now); return
        if self.conn.execute("SELECT 1 FROM mirror_discord_inbound_dispositions WHERE discord_message_id=?", (mid,)).fetchone():
            self._complete(mid, now); return
        # A prior dispatch is never repeated. It waits until its outbox operation appears.
        if correlation:
            self._retry(mid, "response outbox not queued yet", now, awaiting=True); return
        try:
            result = self.handler(PendingInbound(mid, str(row["thread_id"]), payload, int(row["attempt_count"])))
            if inspect.isawaitable(result): result = await result
            if result.outcome == "disposition":
                self._disposition(mid, result.disposition or "filtered", result.detail, result.correlation_id, now)
            elif result.outcome == "routed" and result.correlation_id:
                self.conn.execute("UPDATE mirror_discord_inbound_state SET processing_status='awaiting_response',correlation_id=?,lease_expires_at=NULL,next_attempt_at=? WHERE discord_message_id=?",
                                  (result.correlation_id, now + 1, mid)); self.conn.commit()
            else: self._retry(mid, result.detail or "routing unavailable", now)
        except Exception as exc: self._retry(mid, f"{type(exc).__name__}: {exc}", now)

    def _retry(self, mid: str, error: str, now: int, *, awaiting: bool = False) -> None:
        attempts = int(self.conn.execute("SELECT attempt_count FROM mirror_discord_inbound_state WHERE discord_message_id=?", (mid,)).fetchone()[0]) + 1
        delay = min(self.max_backoff, 2 ** min(attempts, 8))
        status = "awaiting_response" if awaiting else "pending"
        self.conn.execute("UPDATE mirror_discord_inbound_state SET processing_status=?,attempt_count=?,last_error=?,next_attempt_at=?,lease_expires_at=NULL WHERE discord_message_id=?",
                          (status, attempts, error[:1000], now + delay, mid)); self.conn.commit()

    def _complete(self, mid: str, now: int) -> None:
        self.conn.execute("UPDATE mirror_discord_inbound_state SET processing_status='processed',processed_at=?,lease_expires_at=NULL,last_error=NULL WHERE discord_message_id=?", (now, mid)); self.conn.commit()

    def _disposition(self, mid: str, disposition: str, detail: str | None, correlation: str | None, now: int) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("INSERT OR IGNORE INTO mirror_discord_inbound_dispositions (discord_message_id,correlation_id,disposition,detail,created_at) VALUES (?,?,?,?,?)", (mid, correlation, disposition, detail, now))
            self.conn.execute("UPDATE mirror_discord_inbound_state SET processing_status='processed',processed_at=?,lease_expires_at=NULL,last_error=? WHERE discord_message_id=?", (now, detail, mid))
            self.conn.commit()
        except Exception:
            self.conn.rollback(); raise
