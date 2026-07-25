"""Retained, restartable supervision and content-free mirror health metrics."""
from __future__ import annotations

import asyncio
import random
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable, Mapping, Any


@dataclass
class LoopState:
    state: str = "stopped"
    restarts: int = 0
    last_error: str | None = None


class LoopSupervisor:
    """Own named loop tasks, isolate failures, and await cancellation."""

    def __init__(self, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 jitter: Callable[[], float] = random.random,
                 base_backoff: float = .25, max_backoff: float = 30.0):
        self._sleep, self._jitter = sleep, jitter
        self._base, self._max = base_backoff, max_backoff
        self._tasks: dict[str, asyncio.Task] = {}
        self._states: dict[str, LoopState] = {}
        self._stopping_tasks: set[asyncio.Task] = set()
        self._stop_signals: dict[asyncio.Task, asyncio.Event] = {}

    def start(self, name: str, runner: Callable[[], Awaitable[None]]) -> asyncio.Task:
        current = self._tasks.get(name)
        if (current is not None and not current.done()
                and current not in self._stopping_tasks):
            return current
        draining = tuple(self._stopping_tasks)
        stop_signal = asyncio.Event()
        state = self._states.setdefault(name, LoopState())

        async def supervise() -> None:
            failures = 0
            try:
                # A reconnect can race the prior disconnect's cancellation
                # drain. Publish this replacement only after that generation
                # has finished and set its final stopped state.
                if draining:
                    await asyncio.gather(*draining, return_exceptions=True)
                while not stop_signal.is_set():
                    state.state = "running"
                    try:
                        await runner()
                        if stop_signal.is_set():
                            break
                        raise RuntimeError("loop exited unexpectedly")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        failures += 1
                        state.restarts += 1
                        state.last_error = f"{type(exc).__name__}: {exc}"[:300]
                        state.state = "backoff"
                        delay = min(self._max, self._base * (2 ** min(failures - 1, 16)))
                        await self._sleep(delay * (.75 + .5 * self._jitter()))
            finally:
                state.state = "stopped"

        task = asyncio.create_task(supervise(), name=f"kanban-mirror:{name}")
        self._tasks[name] = task
        self._stop_signals[task] = stop_signal
        task.add_done_callback(
            lambda done, generation=name: self._forget_generation(generation, done)
        )
        return task

    def _forget_generation(self, name: str, task: asyncio.Task) -> None:
        """Discard one finished generation without clobbering its replacement."""
        self._stopping_tasks.discard(task)
        self._stop_signals.pop(task, None)
        if self._tasks.get(name) is task:
            self._tasks.pop(name, None)

    async def stop(self) -> None:
        tasks = dict(self._tasks)
        active = [task for task in tasks.values() if not task.done()]
        self._stopping_tasks.update(active)
        for task in active:
            signal = self._stop_signals.get(task)
            if signal is not None:
                signal.set()
            task.cancel()
        try:
            if active:
                await asyncio.gather(*active, return_exceptions=True)
        finally:
            for name, task in tasks.items():
                self._forget_generation(name, task)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {name: asdict(state) for name, state in sorted(self._states.items())}


def health_snapshot(conn: sqlite3.Connection, *, router_enabled: bool,
                    ingress_connected: bool, adapters: Mapping[str, Any],
                    supervisor: LoopSupervisor, now: int | None = None,
                    backlog_limit: int = 1000) -> dict[str, Any]:
    """Return bounded counts/ages only; never expose payloads, errors, or secrets."""
    if not router_enabled:
        return {}
    stamp = int(time.time()) if now is None else int(now)
    scalar = lambda sql, args=(): conn.execute(sql, args).fetchone()[0]
    pending_in = scalar("SELECT COUNT(*) FROM mirror_discord_inbound_state WHERE processing_status='pending'")
    failed_in = scalar("SELECT COUNT(*) FROM mirror_discord_inbound_state WHERE processing_status='pending' AND attempt_count>0")
    oldest_in = scalar("SELECT MIN(observed_at) FROM mirror_discord_inbound_state WHERE processing_status='pending'")
    out = {row[0]: row[1] for row in conn.execute(
        "SELECT status,COUNT(*) FROM mirror_discord_outbox WHERE status!='delivered' GROUP BY status")}
    oldest_out = scalar("SELECT MIN(created_at) FROM mirror_discord_outbox WHERE status!='delivered'")
    logs = {row[0]: row[1] for row in conn.execute(
        "SELECT status,COUNT(*) FROM mirror_conversation_delivery_chunks WHERE status!='delivered' GROUP BY status")}
    oldest_log = scalar("""SELECT MIN(d.created_at) FROM mirror_conversation_delivery_chunks c
      JOIN mirror_conversation_deliveries d USING(operation_id) WHERE c.status!='delivered'""")
    profiles = {row[0] for row in conn.execute(
        "SELECT DISTINCT target_profile FROM mirror_discord_outbox WHERE status!='delivered'") if row[0]}
    cursor_lag = scalar("SELECT COUNT(*) FROM mirror_discord_inbound_state WHERE processing_status='pending'")
    adapter_workers: dict[str, Any] = {}
    for profile, adapter in sorted(adapters.items()):
        snapshot = getattr(adapter, "kanban_supervisor_snapshot", lambda: {})()
        if snapshot:
            adapter_workers[profile] = snapshot
    return {
        "router_enabled": True,
        "discord_ingress_connected": bool(ingress_connected),
        "profile_adapters": {p: bool(adapters.get(p) and getattr(adapters[p], "is_connected", False)) for p in sorted(profiles)},
        "cursor": {"lag": min(cursor_lag, backlog_limit), "backlog_limited": cursor_lag > backlog_limit},
        "pending_inbound": {"count": pending_in, "failed": failed_in,
                            "oldest_age_seconds": None if oldest_in is None else max(0, stamp-oldest_in)},
        "outbox": {"pending": out.get("pending", 0), "failed": out.get("failed", 0),
                   "confirmation_needed": out.get("confirmation_needed", 0),
                   "quarantined": out.get("quarantined", 0),
                   "oldest_age_seconds": None if oldest_out is None else max(0, stamp-oldest_out),
                   "leases": scalar("SELECT COUNT(*) FROM mirror_discord_outbox WHERE lease_expires_at>?", (stamp,))},
        "log_deliveries": {"pending": logs.get("pending", 0), "failed": logs.get("failed", 0),
                           "oldest_age_seconds": None if oldest_log is None else max(0, stamp-oldest_log),
                           "leases": scalar("SELECT COUNT(*) FROM mirror_conversation_delivery_chunks WHERE lease_expires_at>?", (stamp,))},
        "pending_transitions": scalar("SELECT COUNT(*) FROM mirror_transition_recovery WHERE status NOT IN ('delivered','quarantined')"),
        "findings": {"open": scalar("SELECT COUNT(*) FROM mirror_reconciliation_findings WHERE resolved_at IS NULL"),
                     "quarantined": scalar("SELECT COUNT(*) FROM mirror_thread_quarantine WHERE resolved_at IS NULL")},
        "lifecycle_pending": scalar("SELECT COUNT(*) FROM mirror_terminal_lifecycles WHERE state NOT IN ('archived','cancelled')"),
        "supervisor": supervisor.snapshot(),
        "adapter_supervisors": adapter_workers,
    }
