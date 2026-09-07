"""Fixtures for cron_calendar_mirror tests.

Nothing here touches a real Google Calendar or the network. ``FakeCalendar`` is
an in-memory stand-in that reproduces the two Calendar behaviours the plugin
actually depends on: deterministic client-supplied event ids, and ids staying
reserved after a delete.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

import pytest

from plugins.cron_calendar_mirror.calendar_client import (
    CalendarConflict,
    CalendarError,
    CalendarNotFound,
)
from plugins.cron_calendar_mirror.mirror import PROP_PROFILE, Settings

UTC = timezone.utc


class FakeCalendar:
    """In-memory Calendar with the failure modes the reconciler must handle."""

    def __init__(
        self,
        *,
        calendar_id: str = "hermes-crons@group.calendar.google.com",
        summary: str = "Hermes crons",
        access_role: str = "owner",
        primary: bool = False,
    ) -> None:
        self.calendar_id = calendar_id
        self.summary = summary
        self.access_role = access_role
        self.primary = primary
        self.events: Dict[str, Dict[str, Any]] = {}
        self.reserved: set = set()
        self.calls: List[str] = []
        # name -> exception to raise on the next matching call
        self.fail_next: Dict[str, BaseException] = {}
        self.fail_ids: Dict[str, BaseException] = {}

    # -- helpers -----------------------------------------------------------

    def _maybe_fail(self, operation: str, event_id: Optional[str] = None) -> None:
        exc = self.fail_next.pop(operation, None)
        if exc is None and event_id is not None:
            # Keyed by operation as well as id so a test can fail the read-back
            # of an event whose insert it also drove.
            exc = self.fail_ids.pop(f"{operation}:{event_id}", None)
        if exc is not None:
            raise exc

    def stored(self, event_id: str) -> Dict[str, Any]:
        return self.events[event_id]

    def ids_of_kind(self, kind: str) -> List[str]:
        from plugins.cron_calendar_mirror.mirror import PROP_KIND

        return sorted(
            key
            for key, event in self.events.items()
            if event["extendedProperties"]["private"].get(PROP_KIND) == kind
        )

    # -- client surface ----------------------------------------------------

    def verify(self) -> Dict[str, Any]:
        self.calls.append("verify")
        self._maybe_fail("verify")
        return {
            "id": self.calendar_id,
            "summary": self.summary,
            "accessRole": self.access_role,
            "primary": self.primary,
            "source": "calendarList",
            "aclVerified": True,
            "approvedOwner": "owner@example.test",
        }

    def get_event(self, event_id: str) -> Dict[str, Any]:
        self.calls.append(f"get:{event_id}")
        self._maybe_fail("get", event_id)
        if event_id in self.events:
            return dict(self.events[event_id])
        if event_id in self.reserved:
            return {"id": event_id, "status": "cancelled"}
        raise CalendarNotFound(f"event {event_id} not found")

    def list_events(
        self,
        *,
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
        private_property: Sequence[str] = (),
    ) -> List[Dict[str, Any]]:
        self.calls.append("list")
        self._maybe_fail("list")
        wanted = set(private_property)
        items = []
        for event in self.events.values():
            private = event["extendedProperties"]["private"]
            tags = {f"{key}={value}" for key, value in private.items()}
            if wanted and not wanted.issubset(tags):
                continue
            items.append(dict(event))
        return items

    def insert_event(self, body: Dict[str, Any]) -> Dict[str, Any]:
        event_id = body["id"]
        self.calls.append(f"insert:{event_id}")
        self._maybe_fail("insert", event_id)
        if event_id in self.events or event_id in self.reserved:
            raise CalendarConflict(f"duplicate id {event_id}")
        stored = dict(body)
        stored.setdefault("status", "confirmed")
        self.events[event_id] = stored
        return dict(stored)

    def patch_event(self, event_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(f"patch:{event_id}")
        self._maybe_fail("patch", event_id)
        if event_id in self.events:
            current = self.events[event_id]
        elif event_id in self.reserved:
            raise CalendarNotFound("410 tombstoned event")
        else:
            raise CalendarNotFound(f"event {event_id} not found")
        current.update(body)
        if current.get("status") == "confirmed":
            self.reserved.discard(event_id)
        self.events[event_id] = current
        return dict(current)

    def delete_event(self, event_id: str) -> None:
        raise AssertionError("Calendar history must never be deleted")

    # -- assertions --------------------------------------------------------

    def write_calls(self) -> List[str]:
        return [
            call
            for call in self.calls
            if call.split(":", 1)[0] in {"insert", "patch", "delete"}
        ]


@pytest.fixture(autouse=True)
def _fixed_timezone(monkeypatch):
    """Pin the Hermes timezone so cron's own wall-clock maths is deterministic.

    ``cron.jobs`` evaluates cron expressions in the configured Hermes zone, so
    without this the expected occurrence times would depend on the machine's
    local zone. Individual tests override the value to exercise other zones.
    """
    import hermes_time

    monkeypatch.setenv("HERMES_TIMEZONE", "UTC")
    hermes_time.reset_cache()
    yield
    hermes_time.reset_cache()


@pytest.fixture
def fake_calendar() -> FakeCalendar:
    return FakeCalendar()


@pytest.fixture
def settings() -> Settings:
    return Settings(calendar_id="hermes-crons@group.calendar.google.com", approved_owner="owner@example.test")


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def make_job(
    job_id: str,
    *,
    name: str = "Nightly digest",
    schedule: Optional[Dict[str, Any]] = None,
    next_run_at: Optional[datetime] = None,
    enabled: bool = True,
    state: str = "scheduled",
    paused_at: Optional[str] = None,
    repeat_times: Optional[int] = None,
    repeat_completed: int = 0,
    created_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    schedule = schedule or {"kind": "cron", "expr": "0 9 * * *", "display": "every day at 9am"}
    return {
        "id": job_id,
        "name": name,
        "prompt": "internal prompt text that must never reach the calendar",
        "schedule": schedule,
        "schedule_display": schedule.get("display", ""),
        "enabled": enabled,
        "state": state,
        "paused_at": paused_at,
        "created_at": (created_at or datetime(2026, 1, 1, tzinfo=UTC)).isoformat(),
        "next_run_at": next_run_at.isoformat() if next_run_at else None,
        "repeat": {"times": repeat_times, "completed": repeat_completed},
    }


def make_execution(
    execution_id: str,
    job_id: str,
    *,
    claimed_at: datetime,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
    status: str = "completed",
    error: Optional[str] = None,
    source: str = "scheduler",
) -> Dict[str, Any]:
    return {
        "id": execution_id,
        "job_id": job_id,
        "source": source,
        "status": status,
        "claimed_at": claimed_at.isoformat(),
        "started_at": (started_at or claimed_at).isoformat(),
        "finished_at": finished_at.isoformat() if finished_at else None,
        "error": error,
        "pid": 4242,
        "process_id": "abc123",
        "process_started_at": None,
    }


__all__ = [
    "CalendarError",
    "CalendarNotFound",
    "FakeCalendar",
    "PROP_PROFILE",
    "UTC",
    "make_execution",
    "make_job",
    "timedelta",
]
