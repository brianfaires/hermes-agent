"""Deterministic, optional cron-to-Calendar reconciliation.

Forecasts project the scheduler's next_run_at; actual events bind to durable
execution IDs and retained final responses. Reconciliation never drives cron.
Deterministic IDs and readback permit retries without a local sync cursor.
Stale forecasts are archived in place; run history and user titles survive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

PLUGIN_ID = "cron_calendar_mirror"

# extendedProperties.private keys. The profile key is also the list filter, so
# two profiles can share one calendar without pruning each other's events.
PROP_PROFILE = "hermesMirrorProfile"
PROP_KIND = "hermesMirrorKind"
PROP_JOB = "hermesMirrorJobId"
PROP_EXECUTION = "hermesMirrorExecutionId"
PROP_OCCURRENCE = "hermesMirrorOccurrence"
PROP_STATUS = "hermesMirrorStatus"
PROP_VERSION = "hermesMirrorVersion"

SCHEMA_VERSION = "1"

KIND_SCHEDULE = "schedule"
KIND_DIGEST = "digest"
KIND_RUN = "run"
FORECAST_KINDS = frozenset({KIND_SCHEDULE, KIND_DIGEST})

# Google event colour ids.
COLOR_SCHEDULE = "1"   # lavender
COLOR_DIGEST = "1"
COLOR_OK = "10"        # basil
COLOR_FAILED = "11"    # tomato
COLOR_UNKNOWN = "8"    # graphite
COLOR_INFLIGHT = "5"   # banana

_STATUS_COLORS = {
    "completed": COLOR_OK,
    "failed": COLOR_FAILED,
    "unknown": COLOR_UNKNOWN,
    "running": COLOR_INFLIGHT,
    "claimed": COLOR_INFLIGHT,
}

MIN_RUN_EVENT_SECONDS = 60
MAX_SUMMARY_CHARS = 180
MAX_ERROR_CHARS = 400
# Hard stop on occurrence iteration so a pathological schedule cannot spin.
_MAX_PROJECTION_STEPS = 5000
# Occurrences at or just before "now" are kept: the scheduler's next_run_at can
# legitimately sit a few seconds in the past between ticks.
_PAST_OCCURRENCE_GRACE = timedelta(minutes=2)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    calendar_id: str
    approved_owner: str = ""
    horizon_days: int = 7
    max_occurrences_per_job: int = 32
    high_frequency_minutes: int = 60
    result_page_size: int = 200
    include_error_detail: bool = True

    @property
    def horizon(self) -> timedelta:
        return timedelta(days=self.horizon_days)



class ConfigurationError(RuntimeError):
    """The plugin is not configured well enough to touch any calendar."""


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def raw_settings() -> Dict[str, Any]:
    """Read ``plugins.entries.<id>.settings`` without a plugin context."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception as exc:  # pragma: no cover - config layer failure
        logger.debug("cron_calendar_mirror: config unreadable: %s", exc)
        return {}
    node: Any = config
    for key in ("plugins", "entries", PLUGIN_ID, "settings"):
        if not isinstance(node, Mapping):
            return {}
        node = node.get(key)
    return dict(node) if isinstance(node, Mapping) else {}


def load_settings(overrides: Optional[Mapping[str, Any]] = None) -> Settings:
    values: Dict[str, Any] = dict(raw_settings())
    for key, value in (overrides or {}).items():
        if value is not None:
            values[key] = value

    calendar_id = str(values.get("calendar_id") or "").strip()
    if not calendar_id:
        raise ConfigurationError(
            f"plugins.entries.{PLUGIN_ID}.settings.calendar_id is not set. "
            "Point it at a Hermes-owned secondary calendar id."
        )
    if calendar_id.lower() == "primary":
        raise ConfigurationError("calendar_id must not be 'primary'")

    return Settings(
        calendar_id=calendar_id,
        approved_owner=str(values.get("approved_owner") or "").strip(),
        horizon_days=_clamp(values.get("horizon_days"), 7, 1, 31),
        max_occurrences_per_job=_clamp(values.get("max_occurrences_per_job"), 32, 1, 200),
        high_frequency_minutes=_clamp(values.get("high_frequency_minutes"), 60, 1, 1440),
        result_page_size=_clamp(values.get("result_page_size"), 200, 1, 500),
        include_error_detail=bool(values.get("include_error_detail", True)),
    )


# ---------------------------------------------------------------------------
# Identity and time helpers
# ---------------------------------------------------------------------------

def profile_identity() -> str:
    """Stable per-profile marker used in event ids and the list filter."""
    from hermes_constants import get_hermes_home

    return hashlib.sha256(str(get_hermes_home().resolve()).encode()).hexdigest()[:24]


def event_id(profile: str, kind: str, *parts: str) -> str:
    """Deterministic, Google-legal (base32hex) event id.

    Google requires event ids to be 5-1024 characters drawn from ``a-v0-9``.
    Lowercase hex is a strict subset, so a truncated SHA-256 digest is always
    valid, and the ``hcm`` prefix keeps ids recognisable in the Calendar UI.
    """
    payload = "\x00".join([profile, kind, *[str(p) for p in parts]])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return "hcm" + digest[:29]


def hermes_now() -> datetime:
    from hermes_time import now as _now

    return _now()


def hermes_timezone_name() -> Optional[str]:
    try:
        from hermes_time import get_timezone

        zone = get_timezone()
    except Exception:  # pragma: no cover
        return None
    key = getattr(zone, "key", None)
    return str(key) if key else None


def parse_dt(value: Any, *, default_tz=None) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_tz or hermes_now().tzinfo)
    return parsed


def _clean_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def redact(value: Any) -> str:
    """Redact secrets from anything that leaves the process for Google."""
    text = str(value or "")
    if not text:
        return ""
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True, redact_url_credentials=True)
    except Exception:  # pragma: no cover - redaction layer failure
        # Fail closed: without redaction we do not publish free text at all.
        return "[redaction unavailable]"


# ---------------------------------------------------------------------------
# Desired state
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DesiredEvent:
    id: str
    kind: str
    summary: str
    description: str
    start: Dict[str, str]
    end: Dict[str, str]
    private_props: Dict[str, str]
    color_id: str

    def insert_body(self) -> Dict[str, Any]:
        body = self.patch_body()
        body["summary"] = self.summary
        body["id"] = self.id
        return body

    def patch_body(self) -> Dict[str, Any]:
        """Update body. ``summary`` is deliberately absent.

        Calendar ``patch`` leaves omitted fields untouched, so a title the user
        edited by hand in the Calendar UI survives every subsequent reconcile.
        """
        return {
            "description": self.description,
            "start": dict(self.start),
            "end": dict(self.end),
            "colorId": self.color_id,
            # Forecast and history alike are informational, never busy time.
            "transparency": "transparent",
            "visibility": "private",
            "extendedProperties": {"private": dict(self.private_props)},
        }


def _time_fields(moment: datetime, tz_name: Optional[str]) -> Dict[str, str]:
    fields = {"dateTime": moment.isoformat()}
    if tz_name:
        fields["timeZone"] = tz_name
    return fields


def _all_day_fields(day: date) -> Dict[str, str]:
    return {"date": day.isoformat()}


def _job_label(job: Mapping[str, Any]) -> str:
    name = _clean_text(job.get("name") or job.get("id") or "cron job", MAX_SUMMARY_CHARS)
    return _clean_text(redact(name), MAX_SUMMARY_CHARS) or "cron job"


def _schedule_display(job: Mapping[str, Any]) -> str:
    schedule = job.get("schedule") if isinstance(job.get("schedule"), Mapping) else {}
    return _clean_text(
        job.get("schedule_display") or schedule.get("display") or schedule.get("kind") or "",
        120,
    )


def _remaining_runs(job: Mapping[str, Any]) -> Optional[int]:
    repeat = job.get("repeat")
    if not isinstance(repeat, Mapping):
        return None
    times = repeat.get("times")
    if times is None:
        return None
    try:
        remaining = int(times) - int(repeat.get("completed") or 0)
    except (TypeError, ValueError):
        return None
    return max(0, remaining)


def cadence_minutes(job: Mapping[str, Any], anchor: datetime) -> Optional[float]:
    """Approximate spacing between consecutive occurrences, in minutes."""
    from cron.jobs import compute_next_run

    schedule = job.get("schedule")
    if not isinstance(schedule, Mapping):
        return None
    kind = schedule.get("kind")
    if kind == "interval":
        try:
            return float(schedule.get("minutes") or 0) or None
        except (TypeError, ValueError):
            return None
    if kind == "cron":
        nxt = parse_dt(compute_next_run(dict(schedule), last_run_at=anchor.isoformat()))
        if nxt is None or nxt <= anchor:
            return None
        return (nxt - anchor).total_seconds() / 60.0
    return None


def _projection_anchor(job: Mapping[str, Any], now: datetime) -> Optional[datetime]:
    """First occurrence to mirror, in the scheduler's own phase.

    ``next_run_at`` is authoritative. A stale anchor (scheduler stopped, clock
    jumped) is fast-forwarded through the scheduler's own ``compute_next_run``
    rather than by locally re-deriving a phase.
    """
    from cron.jobs import compute_next_run

    anchor = parse_dt(job.get("next_run_at"))
    if anchor is None:
        return None
    if anchor >= now - _PAST_OCCURRENCE_GRACE:
        return anchor
    schedule = job.get("schedule")
    if not isinstance(schedule, Mapping) or schedule.get("kind") == "once":
        return None
    if schedule.get("kind") == "interval":
        from cron.jobs import _ensure_aware

        minutes = float(schedule.get("minutes") or 0)
        if not math.isfinite(minutes) or minutes <= 0:
            return None
        anchor = _ensure_aware(anchor)
        local_now = now.astimezone(anchor.tzinfo)
        elapsed = (local_now.replace(tzinfo=None) - anchor.replace(tzinfo=None)).total_seconds()
        steps = max(0, math.ceil(elapsed / (minutes * 60)))
        return anchor + timedelta(minutes=minutes * steps)
    return parse_dt(compute_next_run(dict(schedule), last_run_at=now.isoformat()))


def project_occurrences(
    job: Mapping[str, Any], *, now: datetime, horizon_end: datetime, limit: int
) -> List[datetime]:
    """Finite list of upcoming fire times inside the horizon."""
    from cron.jobs import compute_next_run, is_job_runnable, is_terminal_job

    if not is_job_runnable(job) or is_terminal_job(job):
        return []
    remaining = _remaining_runs(job)
    if remaining == 0:
        return []

    anchor = _projection_anchor(job, now)
    if anchor is None or anchor > horizon_end:
        return []

    schedule = job.get("schedule")
    if not isinstance(schedule, Mapping):
        return []
    if schedule.get("kind") == "once":
        return [anchor]

    occurrences: List[datetime] = []
    current = anchor
    for _ in range(_MAX_PROJECTION_STEPS):
        if current > horizon_end or len(occurrences) >= limit:
            break
        if remaining is not None and len(occurrences) >= remaining:
            break
        if current >= now - _PAST_OCCURRENCE_GRACE:
            occurrences.append(current)
        nxt = parse_dt(compute_next_run(dict(schedule), last_run_at=current.isoformat()))
        if nxt is None or nxt <= current:
            break
        current = nxt
    return occurrences


def build_schedule_events(
    jobs: Sequence[Mapping[str, Any]],
    *,
    profile: str,
    settings: Settings,
    now: datetime,
    tz_name: Optional[str],
) -> List[DesiredEvent]:
    horizon_end = now + settings.horizon
    events: List[DesiredEvent] = []

    for job in jobs:
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            continue
        anchor = _projection_anchor(job, now)
        if anchor is None:
            continue

        cadence = cadence_minutes(job, anchor)
        if cadence is not None and cadence < settings.high_frequency_minutes and _remaining_runs(job) is None:
            events.extend(
                _digest_events(
                    job,
                    profile=profile,
                    job_id=job_id,
                    cadence=cadence,
                    first=anchor,
                    now=now,
                    horizon_end=horizon_end,
                )
            )
            continue

        for occurrence in project_occurrences(
            job, now=now, horizon_end=horizon_end, limit=settings.max_occurrences_per_job
        ):
            events.append(
                _schedule_event(
                    job,
                    profile=profile,
                    job_id=job_id,
                    occurrence=occurrence,
                    tz_name=tz_name,
                )
            )
    return events


def _schedule_event(
    job: Mapping[str, Any],
    *,
    profile: str,
    job_id: str,
    occurrence: datetime,
    tz_name: Optional[str],
) -> DesiredEvent:
    key = occurrence.astimezone(_utc()).isoformat()
    label = _job_label(job)
    description = _describe_lines([
        ("Cron job", label),
        ("Job id", job_id),
        ("Schedule", _schedule_display(job)),
        ("Scheduled for", occurrence.isoformat()),
        ("Profile", profile),
        ("Note", "Bounded forecast from next_run_at; timing can change after execution or recovery. Results appear as separate run events."),
    ])
    return DesiredEvent(
        id=event_id(profile, KIND_SCHEDULE, job_id, key),
        kind=KIND_SCHEDULE,
        summary=f"⏱ {label}",
        description=description,
        start=_time_fields(occurrence, tz_name),
        end=_time_fields(occurrence + timedelta(minutes=15), tz_name),
        private_props={
            PROP_PROFILE: profile,
            PROP_KIND: KIND_SCHEDULE,
            PROP_STATUS: "forecast",
            PROP_JOB: job_id,
            PROP_OCCURRENCE: key,
            PROP_VERSION: SCHEMA_VERSION,
        },
        color_id=COLOR_SCHEDULE,
    )


def _digest_events(
    job: Mapping[str, Any],
    *,
    profile: str,
    job_id: str,
    cadence: float,
    first: datetime,
    now: datetime,
    horizon_end: datetime,
) -> List[DesiredEvent]:
    """One all-day event per day for jobs that fire too often to enumerate."""
    from cron.jobs import is_job_runnable, is_terminal_job

    if not is_job_runnable(job) or is_terminal_job(job):
        return []
    if _remaining_runs(job) == 0:
        return []

    label = _job_label(job)
    display = _schedule_display(job) or f"every {cadence:g}m"
    start_day = max(now.date(), first.date())
    events: List[DesiredEvent] = []
    day = start_day
    while day <= horizon_end.date():
        key = day.isoformat()
        events.append(
            DesiredEvent(
                id=event_id(profile, KIND_DIGEST, job_id, key),
                kind=KIND_DIGEST,
                summary=f"⏱ {label} ({display})",
                description=_describe_lines([
                    ("Cron job", label),
                    ("Job id", job_id),
                    ("Schedule", display),
                    ("Profile", profile),
                    ("Note",
                     f"High-frequency job (about every {cadence:g} minutes). "
                     "Individual occurrences are not mirrored; run results "
                     "appear as separate run events."),
                ]),
                start=_all_day_fields(day),
                end=_all_day_fields(day + timedelta(days=1)),
                private_props={
                    PROP_PROFILE: profile,
                    PROP_KIND: KIND_DIGEST,
                    PROP_STATUS: "forecast",
                    PROP_JOB: job_id,
                    PROP_OCCURRENCE: key,
                    PROP_VERSION: SCHEMA_VERSION,
                },
                color_id=COLOR_DIGEST,
            )
        )
        day = day + timedelta(days=1)
    return events


def build_run_events(
    executions: Sequence[Mapping[str, Any]],
    *,
    jobs_by_id: Mapping[str, Mapping[str, Any]],
    profile: str,
    settings: Settings,
    now: datetime,
    tz_name: Optional[str],
) -> List[DesiredEvent]:
    events: List[DesiredEvent] = []

    for record in executions:
        execution_id = str(record.get("id") or "").strip()
        job_id = str(record.get("job_id") or "").strip()
        if not execution_id or not job_id:
            continue
        claimed = parse_dt(record.get("claimed_at"))
        if claimed is None:
            continue

        start = parse_dt(record.get("started_at")) or claimed
        finish = parse_dt(record.get("finished_at"))
        if finish is None or finish <= start:
            finish = start + timedelta(seconds=MIN_RUN_EVENT_SECONDS)

        status = str(record.get("status") or "unknown")
        job = jobs_by_id.get(job_id)
        if record.get("job_name") is not None:
            label = _job_label({"name": record["job_name"], "id": job_id})
            schedule = _schedule_display({"schedule": record.get("schedule")})
        elif job is not None:
            label = _job_label(job)
            schedule = _schedule_display(job)
        else:
            label = f"cron job {job_id[:8]} (removed)"
            schedule = ""

        duration = (finish - start).total_seconds()
        lines: List[Tuple[str, str]] = [
            ("Cron job", label),
            ("Job id", job_id),
            ("Execution id", execution_id),
            ("Status", status),
            ("Source", _clean_text(record.get("source"), 40)),
            ("Claimed at", claimed.isoformat()),
            ("Started at", start.isoformat()),
        ]
        if record.get("finished_at"):
            lines.append(("Finished at", finish.isoformat()))
            lines.append(("Duration", f"{duration:.0f}s"))
        else:
            lines.append(("Finished at", "still in flight at last reconcile"))
        if schedule:
            lines.append(("Schedule", schedule))
        if job is None:
            lines.append(("Note", "The cron job no longer exists; this result is retained history."))
        if record.get("scheduled_at"):
            lines.append(("Scheduled for", str(record["scheduled_at"])))
        if record.get("final_response"):
            result = redact(record["final_response"])
            # Google descriptions are bounded. Keep the full answer in the local
            # ledger and make any display truncation explicit.
            if len(result) > 6000:
                result = result[:6000] + "\n[Truncated; full result retained in cron execution ledger]"
            lines.append(("Result", result))
        if settings.include_error_detail and record.get("error"):
            lines.append(("Error", _clean_text(redact(record.get("error")), MAX_ERROR_CHARS)))

        events.append(
            DesiredEvent(
                id=event_id(profile, KIND_RUN, execution_id),
                kind=KIND_RUN,
                summary=f"▶ {label}",
                description=_describe_lines(lines),
                start=_time_fields(start, tz_name),
                end=_time_fields(finish, tz_name),
                private_props={
                    PROP_PROFILE: profile,
                    PROP_KIND: KIND_RUN,
                    PROP_JOB: job_id,
                    PROP_EXECUTION: execution_id,
                    PROP_STATUS: status,
                    PROP_VERSION: SCHEMA_VERSION,
                },
                color_id=_STATUS_COLORS.get(status, COLOR_UNKNOWN),
            )
        )
    return events


def _describe_lines(pairs: Sequence[Tuple[str, str]]) -> str:
    return "\n".join(f"{key}: {value}" for key, value in pairs if str(value).strip())


def _utc():
    from datetime import timezone

    return timezone.utc


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

@dataclass
class ReconcileReport:
    profile: str = ""
    calendar_id: str = ""
    calendar_summary: str = ""
    dry_run: bool = False
    horizon_days: int = 0
    jobs: int = 0
    desired: int = 0
    observed: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    archived: int = 0
    kept_history: int = 0
    failures: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> Dict[str, Any]:
        data = dict(self.__dict__)
        data["ok"] = self.ok
        return data

    def render(self) -> str:
        head = "dry run — no calendar writes" if self.dry_run else "applied"
        lines = [
            f"cron_calendar_mirror ({head})",
            f"  profile        : {self.profile}",
            f"  calendar       : {self.calendar_summary or self.calendar_id}",
            f"  horizon        : {self.horizon_days}d",
            f"  cron jobs      : {self.jobs}",
            f"  desired events : {self.desired} (found {self.observed} managed)",
            f"  created        : {self.created}",
            f"  updated        : {self.updated}",
            f"  unchanged      : {self.unchanged}",
            f"  archived forecast: {self.archived}",
            f"  kept history   : {self.kept_history}",
        ]
        if self.failures:
            lines.append(f"  failures       : {len(self.failures)}")
            lines.extend(f"    - {failure}" for failure in self.failures[:10])
        return "\n".join(lines)


def verify_target(client, settings: Settings) -> Dict[str, Any]:
    """Confirm the target is a writable, non-primary secondary calendar.

    Nothing is written until this passes. A read failure here is fatal for the
    run (we cannot prove the target), never a reason to fall back to another
    calendar.
    """
    info = client.verify()
    if (info.get("id") != settings.calendar_id
            or not settings.calendar_id.endswith("@group.calendar.google.com")
            or info.get("primary") is not False
            or info.get("accessRole") != "owner"
            or not settings.approved_owner
            or info.get("approvedOwner") != settings.approved_owner
            or info.get("aclVerified") is not True):
        raise ConfigurationError("target routing and approved owner-only ACL not verified")
    return info


def _instant(fields: Mapping[str, Any]) -> Optional[str]:
    """Normalise a Calendar start/end block for comparison."""
    if not isinstance(fields, Mapping):
        return None
    if fields.get("date"):
        return f"date:{fields['date']}"
    moment = parse_dt(fields.get("dateTime"))
    return f"time:{moment.astimezone(_utc()).isoformat()}" if moment else None


def needs_update(observed: Mapping[str, Any], desired: DesiredEvent) -> bool:
    """True when the remote event materially differs from the desired one.

    Comparing before writing is what makes a second reconcile a no-op, which is
    what makes retries safe: nothing is rewritten just because we ran again.
    """
    if str(observed.get("status") or "confirmed") == "cancelled":
        return True
    if _instant(observed.get("start") or {}) != _instant(desired.start):
        return True
    if _instant(observed.get("end") or {}) != _instant(desired.end):
        return True
    if str(observed.get("description") or "") != desired.description:
        return True
    if str(observed.get("colorId") or "") != desired.color_id:
        return True
    if str(observed.get("transparency") or "") != "transparent":
        return True
    if str(observed.get("visibility") or "") != "private":
        return True
    props = observed.get("extendedProperties")
    private = props.get("private") if isinstance(props, Mapping) else None
    private = private if isinstance(private, Mapping) else {}
    for key, value in desired.private_props.items():
        if str(private.get(key) or "") != value:
            return True
    return False


def _observed_kind(event: Mapping[str, Any]) -> str:
    props = event.get("extendedProperties")
    private = props.get("private") if isinstance(props, Mapping) else None
    if isinstance(private, Mapping):
        return str(private.get(PROP_KIND) or "")
    return ""


def _check_existing(current, desired):
    from plugins.cron_calendar_mirror.calendar_client import CalendarError

    if (current.get("id") != desired.id or current.get("status") == "cancelled"
            or current.get("visibility") != "private" or current.get("attendees")
            or current.get("attendeesOmitted") or current.get("recurrence")
            or current.get("recurringEventId")):
        raise CalendarError("existing event privacy/integrity not verified")
    actual = current.get("extendedProperties", {}).get("private", {})
    for key in (PROP_PROFILE, PROP_KIND, PROP_JOB, PROP_EXECUTION, PROP_OCCURRENCE):
        if actual.get(key) != desired.private_props.get(key):
            raise CalendarError("existing event identity mismatch")


def _apply_event(client, desired: DesiredEvent, report: ReconcileReport) -> None:
    from plugins.cron_calendar_mirror.calendar_client import CalendarConflict

    try:
        client.insert_event(desired.insert_body())
        created = True
    except CalendarConflict:
        current = client.get_event(desired.id)
        _check_existing(current, desired)
        if needs_update(current, desired):
            client.patch_event(desired.id, desired.patch_body())
        created = False
    current = client.get_event(desired.id)
    _check_existing(current, desired)
    if needs_update(current, desired):
        raise RuntimeError("event write readback did not match")
    if created:
        report.created += 1
    else:
        report.updated += 1


def reconcile(
    client,
    *,
    settings: Settings,
    jobs: Sequence[Mapping[str, Any]],
    executions: Sequence[Mapping[str, Any]],
    profile: str,
    now: Optional[datetime] = None,
    tz_name: Optional[str] = None,
    dry_run: bool = False,
) -> ReconcileReport:
    """Make the calendar match the current cron state. Idempotent."""
    from plugins.cron_calendar_mirror.calendar_client import CalendarError, CalendarNotFound

    now = now or hermes_now()
    report = ReconcileReport(
        profile=profile,
        calendar_id=settings.calendar_id,
        dry_run=dry_run,
        horizon_days=settings.horizon_days,
        jobs=len(jobs),
    )

    info = verify_target(client, settings)
    report.calendar_id = str(info.get("id") or settings.calendar_id)
    report.calendar_summary = _clean_text(info.get("summary"), 120)

    jobs_by_id = {str(job.get("id")): job for job in jobs if job.get("id")}
    desired_events = build_schedule_events(
        jobs, profile=profile, settings=settings, now=now, tz_name=tz_name
    ) + build_run_events(
        executions,
        jobs_by_id=jobs_by_id,
        profile=profile,
        settings=settings,
        now=now,
        tz_name=tz_name,
    )
    desired = {event.id: event for event in desired_events}
    report.desired = len(desired)

    # Read every managed event, including old results and projections beyond a
    # newly shortened horizon. The worker paginates; no age cutoff loses history.
    observed_events = client.list_events(private_property=[f"{PROP_PROFILE}={profile}"])
    observed = {
        str(event.get("id")): event for event in observed_events if event.get("id")
    }
    report.observed = len(observed)

    for identifier, event in sorted(desired.items()):
        current = observed.get(identifier)
        if current is not None:
            try:
                _check_existing(current, event)
            except CalendarError as exc:
                report.failures.append(f"{event.kind} {identifier}: {exc}")
                continue
        if current is not None and not needs_update(current, event):
            report.unchanged += 1
            continue
        if dry_run:
            if current is None:
                report.created += 1
            else:
                report.updated += 1
            continue
        try:
            if current is None:
                _apply_event(client, event, report)
            else:
                client.patch_event(identifier, event.patch_body())
                readback = client.get_event(identifier)
                _check_existing(readback, event)
                if needs_update(readback, event):
                    raise CalendarError("event patch readback did not match")
                report.updated += 1
        except (CalendarError, RuntimeError) as exc:
            # No local state was advanced, so the next reconcile retries this
            # exact event. One bad event never stops the rest of the sync.
            report.failures.append(f"{event.kind} {identifier}: {exc}")

    for identifier, event in sorted(observed.items()):
        if identifier in desired:
            continue
        kind = _observed_kind(event)
        if kind not in FORECAST_KINDS:
            # Run results (and anything we do not recognise) are history.
            report.kept_history += 1
            continue
        props = event.get("extendedProperties", {}).get("private", {})
        if props.get(PROP_STATUS) == "archived":
            report.kept_history += 1
            continue
        if dry_run:
            report.archived += 1
            continue
        try:
            body = {
                "description": "Archived forecast: no longer in the current schedule projection.",
                "colorId": COLOR_UNKNOWN,
                "extendedProperties": {"private": dict(props, **{PROP_STATUS: "archived"})},
            }
            client.patch_event(identifier, body)
            readback = client.get_event(identifier)
            if readback.get("extendedProperties", {}).get("private", {}).get(PROP_STATUS) != "archived":
                raise CalendarError("archive readback did not match")
            report.archived += 1
        except CalendarError as exc:
            report.failures.append(f"archive {identifier}: {exc}")

    return report


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def collect_state(settings: Settings) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Read cron jobs and the execution ledger. Never mutates either."""
    from cron.executions import list_executions, iter_execution_results
    from cron.jobs import load_jobs

    jobs = [job for job in load_jobs() if isinstance(job, dict)]
    retained = list(iter_execution_results(page_size=settings.result_page_size))
    retained_ids = {row["id"] for row in retained}
    # Legacy/in-flight status only. Durable terminal results are never capped.
    executions = retained + [row for row in list_executions(limit=500) if row["id"] not in retained_ids]
    return jobs, executions


def run_sync(
    *,
    overrides: Optional[Mapping[str, Any]] = None,
    dry_run: bool = False,
) -> ReconcileReport:
    from plugins.cron_calendar_mirror.calendar_client import CalendarClient

    settings = load_settings(overrides)
    jobs, executions = collect_state(settings)
    client = CalendarClient(settings.calendar_id, approved_owner=settings.approved_owner)
    return reconcile(
        client,
        settings=settings,
        jobs=jobs,
        executions=executions,
        profile=profile_identity(),
        tz_name=hermes_timezone_name(),
        dry_run=dry_run,
    )


def status_text() -> str:
    """Operator-facing readiness summary. Performs no Calendar calls."""
    from plugins.cron_calendar_mirror.calendar_client import missing_prerequisites, worker_python

    lines = [f"cron_calendar_mirror — profile {profile_identity()}"]
    try:
        settings = load_settings()
        lines.append(f"  calendar_id      : {settings.calendar_id}")
        lines.append(f"  horizon_days     : {settings.horizon_days}")
        lines.append("  results          : all retained executions (no age/count cutoff)")
        lines.append(f"  approved_owner   : {settings.approved_owner or 'MISSING'}")
        lines.append(f"  high_frequency   : < {settings.high_frequency_minutes}m -> daily digest")
    except ConfigurationError as exc:
        lines.append(f"  NOT CONFIGURED   : {exc}")
    lines.append(f"  worker python    : {worker_python()}")
    missing = missing_prerequisites()
    if missing:
        lines.append("  prerequisites    : MISSING")
        lines.extend(f"    - {item}" for item in missing)
    else:
        lines.append("  prerequisites    : ok (skill, token, worker present)")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    from plugins.cron_calendar_mirror.calendar_client import CalendarError, CalendarUnavailable

    parser = argparse.ArgumentParser(
        prog="cron_calendar_mirror",
        description="Mirror this profile's cron schedule and run results onto a calendar.",
    )
    parser.add_argument("command", choices=("sync", "status"), nargs="?", default="sync")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--calendar-id", default=None)
    parser.add_argument("--horizon-days", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "status":
        print(status_text())
        return 0

    overrides = {
        "calendar_id": args.calendar_id,
        "horizon_days": args.horizon_days,
    }
    try:
        report = run_sync(overrides=overrides, dry_run=args.dry_run)
    except (ConfigurationError, CalendarUnavailable) as exc:
        # Fail safe and loud: nothing was written, and cron is untouched.
        print(f"cron_calendar_mirror: not ready — {exc}", file=sys.stderr)
        return 2
    except CalendarError as exc:
        print(f"cron_calendar_mirror: calendar unavailable — {exc}", file=sys.stderr)
        return 2

    print(json.dumps(report.to_dict(), indent=2) if args.json else report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - CLI shim
    raise SystemExit(main())
