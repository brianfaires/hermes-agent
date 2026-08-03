"""Shared, dependency-injected reconciliation for the Hermes cron Calendar projection.

The owning Calendar runner injects credentials, state storage, and Calendar I/O.
Hooks and recovery sweeps both enter this module, so their state transitions
cannot drift.  The engine never discovers a profile home or credentials.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager


class CalendarStateLock:
    """Cross-process lock for one injected Calendar projection state file."""

    def __init__(self, path: Path):
        self.path = path
        self._handle: Any = None
        self._backend: str | None = None

    def __enter__(self) -> "CalendarStateLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                # msvcrt locks one existing byte; a new a+b file is empty.
                if self.path.stat().st_size == 0:
                    self._handle.write(b"0")
                    self._handle.flush()
                    self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
                self._backend = "msvcrt"
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
                self._backend = "fcntl"
        except Exception:
            self._handle.close()
            self._handle = None
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._handle is None:
            return
        try:
            if self._backend == "msvcrt":
                import msvcrt

                self._handle.seek(0)
                getattr(msvcrt, "locking")(self._handle.fileno(), getattr(msvcrt, "LK_UNLCK"), 1)
            elif self._backend == "fcntl":
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
            self._backend = None


@dataclass
class ReconcileContext:
    """Injected policy, state, and I/O boundary for one Calendar projection."""

    state: dict[str, Any]
    dry_run: bool
    calendar_id: str
    should_include: Callable[[dict[str, Any]], bool]
    event_for_job: Callable[[dict[str, Any]], dict[str, Any]]
    signature: Callable[[dict[str, Any]], str]
    get_event: Callable[[str], dict[str, Any]]
    create_event: Callable[[dict[str, Any]], str | None]
    patch_event: Callable[[str, dict[str, Any]], object]
    adopt_event_id: Callable[[str, set[str]], str | None]
    archive_event: Callable[[str, str], object]
    attach_output: Callable[[str, dict[str, Any]], int]
    record_completion: Callable[[dict[str, Any], bool, float | None], None]
    now: Callable[[], str]
    # State handling is optional only for direct unit callers. Production
    # runners inject all three so the engine owns load -> mutate -> save.
    load_state: Callable[[], dict[str, Any]] | None = None
    save_state: Callable[[dict[str, Any]], None] | None = None
    lock: Callable[[], ContextManager[object]] | None = None
    is_stale_error: Callable[[Exception], bool] | None = None
    untracked_event_ids: Callable[[str, set[str]], list[str]] | None = None


def _events(context: ReconcileContext) -> dict[str, dict[str, Any]]:
    events = context.state.setdefault("events", {})
    if not isinstance(events, dict):
        raise ValueError("calendar state events must be an object")
    return events


def _tracked_ids(events: dict[str, dict[str, Any]]) -> set[str]:
    return {
        str(entry.get("event_id"))
        for entry in events.values()
        if isinstance(entry, dict) and entry.get("event_id")
    }


def _missing_error(context: ReconcileContext, exc: Exception) -> bool:
    if context.is_stale_error is not None:
        return context.is_stale_error(exc)
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status in {404, 410} or exc.__class__.__name__ == "CalendarNotFoundError"


def _require_mutation(result: object, operation: str) -> None:
    if result is False:
        raise RuntimeError(f"Calendar {operation} reported failure")


def _patch_body(body: dict[str, Any]) -> dict[str, Any]:
    """Build an in-place update without replacing the user-owned title."""
    patch = dict(body)
    patch.pop("summary", None)
    # Omitting recurrence leaves an existing recurring series unchanged in the
    # Calendar API. An explicit empty list is required for a one-shot update.
    if "recurrence" not in body:
        patch["recurrence"] = []
    return patch


def _archive_untracked_duplicates(
    job_id: str,
    event_id: str,
    events: dict[str, dict[str, Any]],
    context: ReconcileContext,
) -> int:
    if context.dry_run or context.untracked_event_ids is None:
        return 0
    archived = 0
    tracked = _tracked_ids(events)
    for duplicate_id in context.untracked_event_ids(job_id, tracked):
        if duplicate_id == event_id:
            continue
        try:
            _require_mutation(
                context.archive_event(duplicate_id, "duplicate cron series"),
                "duplicate archive",
            )
            archived += 1
        except Exception as exc:
            if _missing_error(context, exc):
                continue
            raise
    return archived


def _adopt_untracked_event(
    job_id: str,
    events: dict[str, dict[str, Any]],
    body: dict[str, Any],
    context: ReconcileContext,
) -> tuple[str | None, int]:
    tracked = _tracked_ids(events)
    untracked_ids = (
        context.untracked_event_ids(job_id, tracked)
        if context.untracked_event_ids is not None
        else []
    )
    candidates = list(untracked_ids)
    if not candidates:
        adopted = context.adopt_event_id(job_id, tracked)
        candidates = [adopted] if adopted else []

    stale_candidates: set[str] = set()
    for candidate_id in candidates:
        try:
            _require_mutation(
                context.patch_event(candidate_id, _patch_body(body)), "adoption patch"
            )
            duplicates_archived = 0
            for duplicate_id in untracked_ids:
                if duplicate_id == candidate_id or duplicate_id in stale_candidates:
                    continue
                try:
                    _require_mutation(
                        context.archive_event(duplicate_id, "duplicate cron series"),
                        "duplicate archive",
                    )
                    duplicates_archived += 1
                except Exception as exc:
                    if _missing_error(context, exc):
                        continue
                    raise
            return candidate_id, duplicates_archived
        except Exception as exc:
            if _missing_error(context, exc):
                stale_candidates.add(candidate_id)
                continue
            raise
    return None, 0


def _reconcile_one(
    job: dict[str, Any],
    operation: str,
    context: ReconcileContext,
    *,
    output_file: str | None = None,
    success: bool = False,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    if operation not in {"create", "update", "remove", "complete"}:
        raise ValueError(f"unsupported cron Calendar operation: {operation}")
    job_id = str(job.get("id") or "")
    if not job_id:
        return {"skipped": 1}
    events = _events(context)
    current = events.get(job_id, {})
    if not isinstance(current, dict):
        current = {}
    event_id = str(current.get("event_id") or "") or None

    finalizing_one_shot = False
    if operation == "complete":
        if (job.get("schedule") or {}).get("kind") != "once":
            # Recurring output is independently valuable. Attach it before a
            # best-effort duration resize so a resize failure cannot lose it.
            attached = context.attach_output(output_file, job) if output_file else 0
            context.record_completion(job, success, duration_seconds)
            return {"output_attached": attached}
        # A one-shot resize patches its master description. Resize first so it
        # cannot erase the final output, then attach before archival.
        context.record_completion(job, success, duration_seconds)
        context.attach_output(output_file, job) if output_file else 0
        finalizing_one_shot = True
        operation = "remove"

    if operation == "remove" or not context.should_include(job):
        if not event_id:
            return {"skipped": 1}
        if (
            operation == "remove"
            and not finalizing_one_shot
            and (job.get("schedule") or {}).get("kind") == "once"
        ):
            # Bounded runs can emit REMOVE before COMPLETE. Retain the event
            # identity until final output has been attached; a later recovery
            # orphan sweep archives entries whose COMPLETE never arrives.
            context.state.setdefault("pending_one_shot_removals", {})[job_id] = {
                "event_id": event_id,
                "name": job.get("name"),
                "queued_at": context.now(),
            }
            return {"deferred": 1}
        if not context.dry_run:
            try:
                _require_mutation(
                    context.archive_event(event_id, "cron is no longer active"), "archive"
                )
            except Exception as exc:
                if _missing_error(context, exc):
                    events.pop(job_id, None)
                    return {"deleted": 1}
                raise
        events.pop(job_id, None)
        context.state.setdefault("pending_one_shot_removals", {}).pop(job_id, None)
        context.state.setdefault("archived_events", {})[job_id] = {
            "event_id": event_id,
            "name": job.get("name"),
            "archived_at": context.now(),
            "reason": "cron is no longer active",
        }
        return {"archived": 1}

    body = context.event_for_job(job)
    desired_signature = context.signature(body)
    if event_id and not context.dry_run:
        try:
            existing = context.get_event(event_id)
            if existing.get("status") == "cancelled":
                event_id = None
                current = {}
        except Exception as exc:
            if _missing_error(context, exc):
                event_id = None
                current = {}
            else:
                raise

    if not event_id:
        adopted: str | None = None
        duplicates_archived = 0
        if context.dry_run:
            event_id = "dry-" + job_id
        else:
            adopted, duplicates_archived = _adopt_untracked_event(
                job_id, events, body, context
            )
            if adopted:
                event_id = adopted
            else:
                event_id = context.create_event(body)
                if not event_id:
                    raise RuntimeError("Calendar create did not return an event ID")
        events[job_id] = {
            "event_id": event_id,
            "signature": desired_signature,
            "name": job.get("name"),
            "profile": job.get("__profile"),
        }
        result = {
            "created": int(not context.dry_run and not adopted),
            "adopted": int(not context.dry_run and bool(adopted)),
        }
        if duplicates_archived:
            result["duplicates_archived"] = duplicates_archived
        return result

    if current.get("signature") != desired_signature:
        if not context.dry_run:
            _require_mutation(context.patch_event(event_id, _patch_body(body)), "patch")
        duplicates_archived = _archive_untracked_duplicates(
            job_id, event_id, events, context
        )
        current.update(
            {
                "event_id": event_id,
                "signature": desired_signature,
                "name": job.get("name"),
                "profile": job.get("__profile"),
            }
        )
        events[job_id] = current
        result = {"updated": 1}
        if duplicates_archived:
            result["duplicates_archived"] = duplicates_archived
        return result
    duplicates_archived = _archive_untracked_duplicates(job_id, event_id, events, context)
    result = {"unchanged": 1}
    if duplicates_archived:
        result["duplicates_archived"] = duplicates_archived
    return result


class ReconciliationEngine:
    """Serializes Calendar mutations with injected state storage and locking."""

    def __init__(self, context: ReconcileContext):
        self.context = context

    @contextlib.contextmanager
    def _transaction(self):
        context = self.context
        if (context.load_state is None) != (context.save_state is None):
            raise ValueError("Calendar state load and save callbacks must be configured together")
        lock = context.lock() if context.lock is not None else contextlib.nullcontext()
        with lock:
            if context.load_state is not None:
                state = context.load_state()
                if not isinstance(state, dict):
                    raise ValueError("calendar state loader must return an object")
                context.state = state
            yield
            if context.save_state is not None and not context.dry_run:
                context.save_state(context.state)

    def reconcile_one(self, job: dict[str, Any], operation: str, **kwargs: Any) -> dict[str, Any]:
        with self._transaction():
            return _reconcile_one(job, operation, self.context, **kwargs)

    def reconcile_orphans(self, jobs: list[dict[str, Any]]) -> dict[str, int]:
        """Archive tracked events absent from the injected recovery inventory."""
        with self._transaction():
            active_ids = {
                str(job.get("id")) for job in jobs if self.context.should_include(job)
            }
            pending_one_shots = self.context.state.get("pending_one_shot_removals", {})
            if not isinstance(pending_one_shots, dict):
                pending_one_shots = {}
            archived = deleted = 0
            for job_id, entry in list(_events(self.context).items()):
                if job_id in active_ids:
                    continue
                pending = pending_one_shots.get(job_id)
                if isinstance(pending, dict) and not pending.get("orphan_sweep_seen_at"):
                    # REMOVE can precede COMPLETE for bounded jobs. COMPLETE
                    # owns final output attachment and subsequent archival. Give
                    # it one recovery sweep to arrive, but do not retain an
                    # orphan forever when COMPLETE never follows.
                    pending["orphan_sweep_seen_at"] = self.context.now()
                    continue
                snapshot = {
                    "id": job_id,
                    "name": entry.get("name") if isinstance(entry, dict) else None,
                    "schedule": {},
                }
                result = _reconcile_one(snapshot, "remove", self.context)
                archived += result.get("archived", 0)
                deleted += result.get("deleted", 0)
            return {"archived": archived, "deleted": deleted}


def reconcile_one(job: dict[str, Any], operation: str, context: ReconcileContext, **kwargs: Any) -> dict[str, Any]:
    """Reconcile one lifecycle snapshot under the engine-owned transaction."""
    return ReconciliationEngine(context).reconcile_one(job, operation, **kwargs)


def reconcile_orphans(jobs: list[dict[str, Any]], context: ReconcileContext) -> dict[str, int]:
    """Sweep tracked series through the same engine used by lifecycle hooks."""
    return ReconciliationEngine(context).reconcile_orphans(jobs)
