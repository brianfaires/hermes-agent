"""cron_calendar_sync — mirror cron jobs onto a Google Calendar.

Registers handlers on the cron lifecycle hooks (see ``cron/hooks.py``):

- CREATE/UPDATE  → upsert the job's calendar event(s). Event length is the
  empirically-learned ``max_duration`` (a default baseline until learned).
  Recurrence is best-effort: a daily/weekly/monthly/yearly schedule becomes one
  recurring event; a sub-daily schedule (which Google Calendar cannot express as
  a single recurrence) is expanded into one DAILY-recurring event per intraday
  slot; anything else falls back to a single next-occurrence event.
- REMOVE         → delete the event(s).
- COMPLETE       → on a successful run, compare the run duration to the stored
  max. If it is larger, grow ``max_duration``, resize the event(s), and notify
  the cron's normal delivery target. The first successful run sets the baseline
  silently. An increase to >= 2x the previous max is escalated to a warning.

Per-job state (event ids, anchor starts, learned max) lives in a sidecar file,
``<cron dir>/calendar_sync.json`` — the cron job record is never touched.

All calendar work is best-effort and routed through ``cron.calendar_client``
(the google-workspace skill); any failure is logged and swallowed so cron job
mutations and runs are never affected.
"""

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import hermes_time
from cron import calendar_client, hooks
from cron.jobs import CRON_DIR

logger = logging.getLogger(__name__)

DEFAULT_CALENDAR = "Hermes crons"
# Event length used before a job has had a successful (timed) run.
DEFAULT_EVENT_SECONDS = 300
# Minimum event length so zero/tiny durations still render a visible block.
MIN_EVENT_SECONDS = 60
# Safety cap on per-day events created for a sub-daily schedule. Schedules with
# more intraday slots than this fall back to a single next-occurrence event.
MAX_DAILY_EVENTS = 144

# cron day-of-week (0 or 7 = Sunday) -> RRULE BYDAY token.
_RRULE_DAYS = {0: "SU", 1: "MO", 2: "TU", 3: "WE", 4: "TH", 5: "FR", 6: "SA", 7: "SU"}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def _config() -> dict:
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        if isinstance(cfg, dict):
            cron_cfg = cfg.get("cron", {}) or {}
            return cron_cfg.get("calendar_sync", {}) or {}
    except Exception:
        pass
    return {}


def _calendar_id() -> str:
    return _config().get("calendar_id") or DEFAULT_CALENDAR


def _enabled() -> bool:
    if not _config().get("enabled", True):
        return False
    return calendar_client.available()


def _iana_timezone() -> Optional[str]:
    tz = hermes_time.get_timezone()
    if tz is not None and getattr(tz, "key", None):
        return tz.key
    local = datetime.now().astimezone().tzinfo
    return getattr(local, "key", None)


# --------------------------------------------------------------------------- #
# Sidecar state
# --------------------------------------------------------------------------- #

def _state_path() -> Path:
    return CRON_DIR / "calendar_sync.json"


def _load_state() -> dict:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("cron_calendar_sync: could not read state %s; starting fresh", path)
        return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".calendar_sync_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.error("cron_calendar_sync: failed to save state: %s", e)
        try:
            os.unlink(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Schedule -> recurring RRULE (daily and coarser only)
# --------------------------------------------------------------------------- #

def _cron_rrule(expr: str) -> Optional[str]:
    """Map daily/weekly/monthly/yearly cron expressions to a Google-valid RRULE.

    Returns None for sub-daily or complex expressions (the caller then expands
    intraday slots or falls back to a single event). Google Calendar rejects
    sub-daily (MINUTELY/HOURLY) frequencies, so those are never produced here.
    """
    fields = (expr or "").split()
    if len(fields) != 5:
        return None
    minute, hour, dom, month, dow = fields

    def is_int(field):
        return field.isdigit()

    # daily at h:m
    if is_int(minute) and is_int(hour) and dom == month == "*":
        if dow == "*":
            return "RRULE:FREQ=DAILY"
        days = []
        for part in dow.split(","):
            if is_int(part) and int(part) in _RRULE_DAYS:
                days.append(_RRULE_DAYS[int(part)])
            else:
                return None
        if days:
            return f"RRULE:FREQ=WEEKLY;BYDAY={','.join(days)}"

    # monthly / yearly on a fixed day-of-month at h:m
    if is_int(minute) and is_int(hour) and is_int(dom) and dow == "*":
        if month == "*":
            return f"RRULE:FREQ=MONTHLY;BYMONTHDAY={int(dom)}"
        if is_int(month):
            return f"RRULE:FREQ=YEARLY;BYMONTH={int(month)};BYMONTHDAY={int(dom)}"

    return None


def _rrule_for_schedule(schedule: dict) -> Optional[str]:
    """RRULE for a daily-or-coarser schedule; None means handle as sub-daily/single."""
    if not isinstance(schedule, dict):
        return None
    kind = schedule.get("kind")
    if kind == "interval":
        try:
            minutes = int(schedule.get("minutes") or 0)
        except (TypeError, ValueError):
            return None
        if minutes > 0 and minutes % 1440 == 0:
            return f"RRULE:FREQ=DAILY;INTERVAL={minutes // 1440}"
        return None
    if kind == "cron":
        return _cron_rrule(schedule.get("expr", ""))
    return None


# --------------------------------------------------------------------------- #
# Sub-daily -> intraday slots (one DAILY event per slot)
# --------------------------------------------------------------------------- #

def _parse_cron_field(field: str, mod: int) -> Optional[List[int]]:
    """Expand a cron minute/hour field to concrete values, or None if unsupported."""
    if field == "*":
        return list(range(mod))
    step = re.fullmatch(r"\*/(\d+)", field)
    if step:
        n = int(step.group(1))
        return list(range(0, mod, n)) if n > 0 else None
    values = []
    for part in field.split(","):
        if part.isdigit():
            v = int(part)
            if 0 <= v < mod:
                values.append(v)
            else:
                return None
        else:
            return None
    return sorted(set(values))


def _slot_iso(anchor: datetime, hour: int, minute: int) -> str:
    return anchor.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()


def _interval_intraday_slots(minutes: int, anchor: datetime) -> Optional[List[str]]:
    """Times-of-day for a sub-daily interval that divides evenly into a day."""
    if minutes <= 0 or minutes >= 1440 or 1440 % minutes != 0:
        return None
    start = anchor.hour * 60 + anchor.minute
    offsets = sorted({(start + i * minutes) % 1440 for i in range(1440 // minutes)})
    return [_slot_iso(anchor, off // 60, off % 60) for off in offsets]


def _cron_intraday_slots(expr: str, anchor: datetime) -> Optional[List[str]]:
    """Times-of-day for a sub-daily cron that fires on every day (dom/mon/dow = *)."""
    fields = (expr or "").split()
    if len(fields) != 5:
        return None
    minute, hour, dom, month, dow = fields
    if not (dom == "*" and month == "*" and dow == "*"):
        return None
    minutes = _parse_cron_field(minute, 60)
    hours = _parse_cron_field(hour, 24)
    if minutes is None or hours is None:
        return None
    slots = {_slot_iso(anchor, h, m) for h in hours for m in minutes}
    return sorted(slots)


def _plan_events(job: dict) -> List[dict]:
    """Desired calendar events for a job: list of {"start", "recurrence"}.

    Empty when there is no scheduled time to place.
    """
    anchor = job.get("next_run_at")
    if not anchor:
        return []
    try:
        anchor_dt = datetime.fromisoformat(anchor)
    except (TypeError, ValueError):
        logger.warning("cron_calendar_sync: bad next_run_at %r for job %s", anchor, job.get("id"))
        return []

    schedule = job.get("schedule", {}) or {}
    kind = schedule.get("kind")

    if kind == "once":
        return [{"start": anchor, "recurrence": None}]

    rrule = _rrule_for_schedule(schedule)
    if rrule:
        return [{"start": anchor, "recurrence": rrule}]

    # Sub-daily: expand into one DAILY-recurring event per intraday slot.
    slots = None
    if kind == "interval":
        try:
            slots = _interval_intraday_slots(int(schedule.get("minutes") or 0), anchor_dt)
        except (TypeError, ValueError):
            slots = None
    elif kind == "cron":
        slots = _cron_intraday_slots(schedule.get("expr", ""), anchor_dt)

    if slots:
        if len(slots) <= MAX_DAILY_EVENTS:
            return [{"start": s, "recurrence": "RRULE:FREQ=DAILY"} for s in slots]
        logger.warning(
            "cron_calendar_sync: %d intraday slots exceeds cap %d for job %s; "
            "using a single next-occurrence event",
            len(slots), MAX_DAILY_EVENTS, job.get("id"),
        )

    return [{"start": anchor, "recurrence": None}]


# --------------------------------------------------------------------------- #
# Event helpers
# --------------------------------------------------------------------------- #

def _event_end(anchor_iso: str, duration_seconds: float) -> Optional[str]:
    """Return ``anchor + max(duration, MIN)`` as an ISO string, or None on parse error."""
    try:
        start = datetime.fromisoformat(anchor_iso)
    except (TypeError, ValueError):
        return None
    seconds = max(float(duration_seconds or 0), MIN_EVENT_SECONDS)
    return (start + timedelta(seconds=seconds)).isoformat()


def _summary(job: dict) -> str:
    return f"⏰ {job.get('name') or job.get('id') or 'cron job'}"


def _description(job: dict) -> str:
    parts = [f"cron job_id: {job.get('id', '')}"]
    disp = job.get("schedule_display")
    if disp:
        parts.append(f"schedule: {disp}")
    return "\n".join(parts)


def _stored_events(entry: dict) -> List[dict]:
    """Return the event list for a state entry (tolerating the old single-event schema)."""
    events = entry.get("events")
    if isinstance(events, list):
        return events
    if entry.get("event_id"):  # legacy single-event entry
        return [{"event_id": entry["event_id"], "start": entry.get("start")}]
    return []


def _delete_events(entry: dict) -> None:
    cal = _calendar_id()
    for ev in _stored_events(entry):
        eid = ev.get("event_id")
        if eid:
            calendar_client.delete_event(cal, eid)


def _resize_events(entry: dict, duration: float) -> None:
    """Resize every event for a job to the new max_duration."""
    cal = _calendar_id()
    tz = _iana_timezone()
    for ev in _stored_events(entry):
        anchor = ev.get("start")
        eid = ev.get("event_id")
        if not anchor or not eid:
            continue
        end = _event_end(anchor, duration)
        if end:
            calendar_client.update_event(cal, eid, start=anchor, end=end, timezone=tz)


def _upsert(job: dict) -> None:
    """(Re)create the calendar event(s) for a job to match its current schedule."""
    job_id = job.get("id")
    state = _load_state()
    entry = state.get(job_id, {})
    learned = entry.get("max_duration_seconds")

    plan = _plan_events(job)
    # Recreate cleanly: drop any existing events first.
    _delete_events(entry)

    if not plan:
        if job_id in state:
            state.pop(job_id, None)
            _save_state(state)
        return

    duration = learned if learned is not None else DEFAULT_EVENT_SECONDS
    tz = _iana_timezone()
    cal = _calendar_id()

    created = []
    for p in plan:
        end = _event_end(p["start"], duration)
        if end is None:
            continue
        eid = calendar_client.create_event(
            cal, _summary(job), p["start"], end,
            recurrence=p["recurrence"], timezone=tz, description=_description(job),
        )
        if eid:
            created.append({"event_id": eid, "start": p["start"], "recurrence": p["recurrence"]})

    if not created:
        if job_id in state:
            state.pop(job_id, None)
            _save_state(state)
        return

    state[job_id] = {
        "events": created,
        "max_duration_seconds": learned,
        "max_duration_updated_at": entry.get("max_duration_updated_at"),
    }
    _save_state(state)


# --------------------------------------------------------------------------- #
# Hook handlers
# --------------------------------------------------------------------------- #

def on_create(job: dict, **_) -> None:
    if not _enabled():
        return
    _upsert(job)


def on_update(job: dict, **_) -> None:
    if not _enabled():
        return
    _upsert(job)


def on_remove(job: dict, **_) -> None:
    if not _enabled():
        return
    job_id = job.get("id")
    state = _load_state()
    entry = state.pop(job_id, None)
    if entry:
        _save_state(state)
        _delete_events(entry)


def on_complete(job: dict, success: bool, duration_seconds: float,
                notify=None, error=None, **_) -> None:
    if not _enabled() or not success or duration_seconds is None:
        return

    try:
        duration = round(float(duration_seconds), 3)
    except (TypeError, ValueError):
        return
    if duration < 0:
        return

    job_id = job.get("id")
    state = _load_state()
    entry = state.get(job_id)
    if not entry:
        # Job isn't tracked (e.g. created before the feature was enabled).
        return

    prior = entry.get("max_duration_seconds")
    now = hermes_time.now().isoformat()

    if prior is None:
        # First successful run: establish the baseline silently.
        entry["max_duration_seconds"] = duration
        entry["max_duration_updated_at"] = now
        state[job_id] = entry
        _save_state(state)
        _resize_events(entry, duration)
        return

    try:
        prior_val = float(prior)
    except (TypeError, ValueError):
        prior_val = None

    if prior_val is None or duration <= prior_val:
        return  # no growth

    # max_duration increased
    entry["max_duration_seconds"] = duration
    entry["max_duration_updated_at"] = now
    state[job_id] = entry
    _save_state(state)
    _resize_events(entry, duration)

    name = job.get("name") or job_id
    warn = duration >= (prior_val * 2)
    if warn:
        message = (
            f"⚠️ cron took longer than expected: [{name}] increased cron "
            f"max_duration from {prior_val:.0f}s -> {duration:.0f}s"
        )
    else:
        message = (
            f"[{name}] increased cron max_duration from "
            f"{prior_val:.0f}s -> {duration:.0f}s"
        )

    if notify is not None:
        try:
            notify(message, warn=warn)
        except Exception as e:
            logger.error("cron_calendar_sync: notify failed for job %s: %s", job_id, e)
    elif warn:
        logger.warning("%s", message)
    else:
        logger.info("%s", message)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

def register() -> None:
    """Register the calendar-sync handlers on the cron lifecycle hooks."""
    hooks.register_hook(hooks.CREATE, on_create)
    hooks.register_hook(hooks.UPDATE, on_update)
    hooks.register_hook(hooks.REMOVE, on_remove)
    hooks.register_hook(hooks.COMPLETE, on_complete)
