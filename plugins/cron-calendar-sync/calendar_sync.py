"""Mirror profile-local cron jobs to Google Calendar through lifecycle hooks.

The plugin keeps cron core calendar-agnostic. Each profile owns its Calendar
credentials and sidecar state. Reconciliation preserves user-edited summaries,
updates managed series in place, archives ended series, and adopts surviving
managed events after interrupted state writes.
"""

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import hermes_time
from cron import hooks
from cron.jobs import CRON_DIR

from . import calendar_client
from .calendar_client import CalendarOperationError

logger = logging.getLogger(__name__)

DEFAULT_CALENDAR = "Hermes crons"
DEFAULT_EVENT_SECONDS = 300
MIN_EVENT_SECONDS = 60
HIGH_FREQUENCY_SECONDS = 6 * 3600
MANAGED_BY = "hermes-cron-calendar-sync"
LEGACY_MANAGED_BY = "hermes-cron-calendar-recurring-sync"
MANAGED_TAG = "Managed by Hermes cron-calendar-sync plugin"
RUN_OUTPUT_TAG = "Hermes cron execution output"
RUN_OUTPUT_RENDER_VERSION = 2
MAX_OUTPUT_CHARS = 3500

SECRET_REDACTION_PATTERNS = [
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
        r"password|passwd|authorization|bearer)\b\s*[:=]\s*([^\s`'\"]{8,})"
    ),
    re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._~+/=-]{12,})"),
    re.compile(r"(?i)\b(authorization)\s*:\s*(?:basic|bearer)\s+\S+"),
    re.compile(r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY)[A-Z0-9_]*)\s*=\s*(\S{8,})"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{20,})"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{20,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{20,})"),
]

_RRULE_DAYS = {
    0: "SU",
    1: "MO",
    2: "TU",
    3: "WE",
    4: "TH",
    5: "FR",
    6: "SA",
    7: "SU",
}


def _config() -> dict:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
        value = (cron_cfg or {}).get("calendar_sync", {})
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _calendar_id() -> str:
    return _config().get("calendar_id") or DEFAULT_CALENDAR


def _enabled() -> bool:
    return bool(_config().get("enabled", True)) and calendar_client.available()


def _iana_timezone() -> Optional[str]:
    tz = hermes_time.get_timezone()
    if tz is not None and getattr(tz, "key", None):
        return tz.key
    return getattr(datetime.now().astimezone().tzinfo, "key", None)


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
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".calendar_sync_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception as exc:
        logger.error("cron_calendar_sync: failed to save state: %s", exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _parse_dt(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_cron_field(field: str, minimum: int, maximum: int) -> Optional[list[int]]:
    field = field.strip()
    if field == "*":
        return None
    values: set[int] = set()
    try:
        for raw_part in field.split(","):
            part = raw_part.strip()
            if not part:
                continue
            step = 1
            if "/" in part:
                base, raw_step = part.split("/", 1)
                step = int(raw_step)
                if step <= 0:
                    return None
            else:
                base = part
            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                raw_start, raw_end = base.split("-", 1)
                start, end = int(raw_start), int(raw_end)
            else:
                start = end = int(base)
            values.update(range(max(start, minimum), min(end, maximum) + 1, step))
    except (TypeError, ValueError):
        return None
    return sorted(values)


def _cron_rrule(expr: str) -> Optional[str]:
    fields = (expr or "").split()
    if len(fields) != 5:
        return None
    minute_f, hour_f, dom_f, month_f, dow_f = fields
    minutes = _parse_cron_field(minute_f, 0, 59)
    hours = _parse_cron_field(hour_f, 0, 23)
    doms = _parse_cron_field(dom_f, 1, 31)
    months = _parse_cron_field(month_f, 1, 12)
    dows = _parse_cron_field(dow_f, 0, 7)
    if doms is not None and dows is not None:
        return None
    if doms is not None:
        bits = ["FREQ=MONTHLY", "BYMONTHDAY=" + ",".join(map(str, doms))]
    elif dows is not None:
        bits = ["FREQ=WEEKLY", "BYDAY=" + ",".join(_RRULE_DAYS[day] for day in dows)]
    else:
        bits = ["FREQ=DAILY"]
    if months is not None:
        bits.append("BYMONTH=" + ",".join(map(str, months)))
    if hours is not None and len(hours) > 1:
        bits.append("BYHOUR=" + ",".join(map(str, hours)))
    if minutes is not None and len(minutes) > 1:
        bits.append("BYMINUTE=" + ",".join(map(str, minutes)))
    elif minutes is not None and (hours is not None and len(hours) > 1):
        bits.append(f"BYMINUTE={minutes[0]}")
    return "RRULE:" + ";".join(bits)


def _rrule_for_schedule(schedule: dict) -> Optional[str]:
    if not isinstance(schedule, dict):
        return None
    if schedule.get("kind") == "interval":
        seconds = _schedule_interval_seconds({"schedule": schedule})
        if seconds and seconds % 86400 == 0:
            return f"RRULE:FREQ=DAILY;INTERVAL={seconds // 86400}"
        return None
    if schedule.get("kind") == "cron":
        return _cron_rrule(schedule.get("expr", ""))
    return None


def _schedule_interval_seconds(job: dict) -> Optional[int]:
    schedule = job.get("schedule") or {}
    kind = schedule.get("kind")
    if kind == "interval":
        for key, multiplier in (("seconds", 1), ("minutes", 60), ("hours", 3600)):
            value = schedule.get(key)
            if value is None:
                continue
            try:
                seconds = int(value) * multiplier
            except (TypeError, ValueError):
                continue
            return seconds if seconds > 0 else None
        display = str(schedule.get("display") or job.get("schedule_display") or "")
        match = re.search(
            r"every\s+(\d+)\s*(s|sec|second|seconds|m|min|minute|minutes|h|hr|hour|hours)\b",
            display,
            re.IGNORECASE,
        )
        if not match:
            return None
        value = int(match.group(1))
        unit = match.group(2).lower()
        return value if unit.startswith("s") else value * (60 if unit.startswith("m") else 3600)
    if kind != "cron":
        return None
    fields = str(schedule.get("expr") or job.get("schedule_display") or "").split()
    if len(fields) != 5:
        return None
    minute_f, hour_f, dom_f, month_f, dow_f = fields
    minutes = _parse_cron_field(minute_f, 0, 59)
    hours = _parse_cron_field(hour_f, 0, 23)
    if minutes is None:
        return 60
    if hours is None:
        if len(minutes) > 1:
            gaps = [b - a for a, b in zip(minutes, minutes[1:])]
            gaps.append((60 - minutes[-1]) + minutes[0])
            return min(gaps) * 60
        return 3600
    fire_minutes = sorted({hour * 60 + minute for hour in hours for minute in minutes})
    if len(fire_minutes) > 1:
        gaps = [b - a for a, b in zip(fire_minutes, fire_minutes[1:])]
        gaps.append((1440 - fire_minutes[-1]) + fire_minutes[0])
        return min(gaps) * 60
    if dom_f == month_f == dow_f == "*":
        return 86400
    return None


def _is_high_frequency_schedule(job: dict) -> bool:
    interval = _schedule_interval_seconds(job)
    return interval is not None and interval <= HIGH_FREQUENCY_SECONDS


def _plan_events(job: dict) -> list[dict]:
    anchor = _parse_dt(job.get("next_run_at"))
    if anchor is None:
        return []
    schedule = job.get("schedule") or {}
    kind = schedule.get("kind")
    if kind == "once":
        end = anchor + timedelta(seconds=DEFAULT_EVENT_SECONDS)
        return [
            {
                "start": {"dateTime": anchor.isoformat(), "timeZone": _iana_timezone()},
                "end": {"dateTime": end.isoformat(), "timeZone": _iana_timezone()},
                "recurrence": None,
                "mode": "one-shot",
            }
        ]
    if _is_high_frequency_schedule(job):
        date = anchor.date()
        return [
            {
                "start": {"date": date.isoformat()},
                "end": {"date": (date + timedelta(days=1)).isoformat()},
                "recurrence": "RRULE:FREQ=DAILY;INTERVAL=1",
                "mode": "all-day-high-frequency",
            }
        ]
    rrule = _rrule_for_schedule(schedule)
    end = anchor + timedelta(seconds=DEFAULT_EVENT_SECONDS)
    return [
        {
            "start": {"dateTime": anchor.isoformat(), "timeZone": _iana_timezone()},
            "end": {"dateTime": end.isoformat(), "timeZone": _iana_timezone()},
            "recurrence": rrule,
            "mode": "recurring" if rrule else "single-next-occurrence",
        }
    ]


def _summary(job: dict) -> str:
    return f"⏰ {job.get('name') or job.get('id') or 'cron job'}"


def _description(job: dict) -> str:
    lines = [
        MANAGED_TAG,
        "",
        f"Cron job: {job.get('name') or ''}",
        f"Job ID: {job.get('id') or ''}",
        f"Schedule: {job.get('schedule_display') or (job.get('schedule') or {}).get('display') or (job.get('schedule') or {}).get('expr') or ''}",
    ]
    return "\n".join(lines)


def _event_body(job: dict, plan: dict) -> dict:
    body = {
        "summary": _summary(job),
        "description": _description(job),
        "start": plan["start"],
        "end": plan["end"],
        "extendedProperties": {
            "private": {
                "managedBy": MANAGED_BY,
                "hermesCronJobId": str(job.get("id") or ""),
                "hermesCronCalendarMode": plan["mode"],
            }
        },
    }
    if plan.get("recurrence"):
        body["recurrence"] = [plan["recurrence"]]
    return body


def _stored_events(entry: dict) -> list[dict]:
    events = entry.get("events") if isinstance(entry, dict) else None
    if isinstance(events, list):
        return events
    if isinstance(entry, dict) and entry.get("event_id"):
        return [{"event_id": entry["event_id"], "start": entry.get("start")}]
    return []


def _managed_job_id(event: dict) -> Optional[str]:
    private = ((event.get("extendedProperties") or {}).get("private") or {})
    managed_by = private.get("managedBy")
    value = private.get("hermesCronJobId")
    if managed_by in {MANAGED_BY, LEGACY_MANAGED_BY} and value:
        return str(value)
    description = event.get("description") or ""
    if MANAGED_TAG in description or "Managed by Hermes cron_calendar_recurring_sync.py" in description:
        match = re.search(r"(?:Job ID:|cron job_id:)\s*([^\s]+)", description)
        return match.group(1) if match else None
    return None


def _adoptable_events(calendar: str, job_id: str) -> list[dict]:
    return [
        event
        for event in calendar_client.list_events(calendar)
        if event.get("status") != "cancelled" and _managed_job_id(event) == job_id
    ]


def _utc_until_before(now: Optional[datetime] = None) -> str:
    moment = (now or hermes_time.now()).astimezone(timezone.utc) - timedelta(seconds=1)
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _truncate_rrule(rrule: str) -> str:
    if not rrule.startswith("RRULE:"):
        return rrule
    parts = [
        part
        for part in rrule[len("RRULE:") :].split(";")
        if part and not part.startswith(("UNTIL=", "COUNT="))
    ]
    parts.append(f"UNTIL={_utc_until_before()}")
    return "RRULE:" + ";".join(parts)


def _archive_event(calendar: str, event_id: str, reason: str) -> bool:
    try:
        event = calendar_client.get_event(calendar, event_id)
    except CalendarOperationError:
        return False
    if not event:
        return True
    body = {}
    recurrence = event.get("recurrence") or []
    if recurrence:
        body["recurrence"] = [_truncate_rrule(recurrence[0])]
    description = event.get("description") or ""
    if "Archived by Hermes cron calendar sync" not in description:
        note = (
            "\n\n---\nArchived by Hermes cron calendar sync: "
            f"{reason}. Past instances are intentionally retained."
        )
        body["description"] = description + note
    return not body or calendar_client.patch_event_body(calendar, event_id, body)


def _upsert(job: dict) -> None:
    job_id = str(job.get("id") or "")
    if not job_id:
        return
    plan = _plan_events(job)
    state = _load_state()
    entry = state.get(job_id, {}) if isinstance(state.get(job_id), dict) else {}
    if not plan:
        return
    calendar = _calendar_id()
    body = _event_body(job, plan[0])

    tracked = _stored_events(entry)
    live_event = None
    event_id = None
    try:
        for tracked_event in tracked:
            candidate_id = tracked_event.get("event_id")
            if candidate_id:
                candidate = calendar_client.get_event(calendar, candidate_id)
                if candidate and candidate.get("status") != "cancelled":
                    event_id, live_event = str(candidate_id), candidate
                    break
        adoptable = _adoptable_events(calendar, job_id)
    except CalendarOperationError as exc:
        logger.warning("cron_calendar_sync: reconciliation read failed for %s: %s", job_id, exc)
        return
    if event_id is None and adoptable:
        live_event = adoptable[0]
        event_id = str(live_event.get("id") or "") or None

    if event_id:
        patch_body = dict(body)
        patch_body.pop("summary", None)
        if "recurrence" not in body and (live_event or {}).get("recurrence"):
            patch_body["recurrence"] = []
        if not calendar_client.patch_event_body(calendar, event_id, patch_body):
            logger.warning("cron_calendar_sync: could not update event %s for %s", event_id, job_id)
            return
    else:
        event_id = calendar_client.create_event_body(calendar, body)
        if not event_id:
            logger.warning("cron_calendar_sync: could not create event for %s", job_id)
            return

    for extra in [*tracked, *adoptable]:
        extra_id = str(extra.get("event_id") or extra.get("id") or "")
        if extra_id and extra_id != event_id:
            _archive_event(calendar, extra_id, "superseded duplicate managed series")

    state[job_id] = {
        "events": [
            {
                "event_id": event_id,
                "start": plan[0]["start"],
                "mode": plan[0]["mode"],
            }
        ],
        "max_duration_seconds": entry.get("max_duration_seconds"),
        "max_duration_updated_at": entry.get("max_duration_updated_at"),
    }
    _save_state(state)


def _resize_event(entry: dict, duration: float) -> bool:
    calendar = _calendar_id()
    try:
        for stored in _stored_events(entry):
            event_id = str(stored.get("event_id") or "")
            event = calendar_client.get_event(calendar, event_id) if event_id else None
            if not event or "dateTime" not in (event.get("start") or {}):
                continue
            start = _parse_dt(event["start"].get("dateTime"))
            if start is None:
                continue
            seconds = max(float(duration or 0), MIN_EVENT_SECONDS)
            if not calendar_client.patch_event_body(
                calendar,
                event_id,
                {
                    "start": event["start"],
                    "end": {
                        "dateTime": (start + timedelta(seconds=seconds)).isoformat(),
                        "timeZone": (event.get("end") or {}).get("timeZone") or _iana_timezone(),
                    },
                },
            ):
                return False
    except CalendarOperationError:
        return False
    return True


def _redact_secrets(text: str) -> str:
    redacted = text
    for pattern in SECRET_REDACTION_PATTERNS:
        redacted = pattern.sub(
            lambda match: (
                f"{match.group(1)}: REDACTED"
                if len(match.groups()) >= 2
                else "REDACTED"
            ),
            redacted,
        )
    return redacted


def _strip_prompt_sections(text: str) -> str:
    value = text.replace("\r\n", "\n")
    value = re.sub(r"(?ms)\n?Prompt preview:\n.*?(?=\n\n---\n|\Z)", "", value)
    value = re.sub(r"(?ms)\n?## Prompt\n.*?(?=\n## (?:Response|Error)\n|\Z)", "", value)
    return _redact_secrets(value).rstrip()


def _extract_final_output(saved_output: str) -> str:
    text = saved_output.replace("\r\n", "\n").strip()
    response = re.search(r"(?ms)^## Response\s*\n(?P<body>.*)$", text)
    if response:
        return response.group("body").strip() or "(No response generated)"
    error = re.search(r"(?ms)^## Error\s*\n(?P<body>.*)$", text)
    if error:
        return "Cron failed:\n" + error.group("body").strip()
    status = re.search(r"(?mi)^\*\*Status:\*\*\s*(.+)$", text)
    if status and "\n---\n" not in text:
        return f"Cron status: {status.group(1).strip()}"
    if "\n---\n" in text:
        return text.split("\n---\n", 1)[1].strip()
    return _strip_prompt_sections(text).strip()


def _output_run_time(path: Path) -> Optional[datetime]:
    match = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})\.md$", path.name)
    if not match:
        return None
    date, hour, minute, second = match.groups()
    parsed = _parse_dt(f"{date}T{hour}:{minute}:{second}")
    if parsed is None:
        return None
    tz = hermes_time.get_timezone()
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None and tz else parsed


def _render_run_output(path: Path) -> str:
    text = _redact_secrets(_extract_final_output(path.read_text(errors="replace")).strip())
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[: MAX_OUTPUT_CHARS - 1].rstrip() + "…"
    return (
        f"\n\n---\n{RUN_OUTPUT_TAG}: {path.name}\n"
        f"Render version: {RUN_OUTPUT_RENDER_VERSION}\n\n{text}"
    )


def _upsert_output_block(existing: str, output_file: Path) -> str:
    clean = _strip_prompt_sections(existing)
    block = _render_run_output(output_file).lstrip("\n")
    marker = re.escape(f"{RUN_OUTPUT_TAG}: {output_file.name}")
    pattern = re.compile(
        rf"(?ms)(?:\n\n)?---\n{marker}\n(?:Render version: \d+\n)?\n.*?"
        rf"(?=(?:\n\n---\n{re.escape(RUN_OUTPUT_TAG)}: )|\Z)"
    )
    if pattern.search(clean):
        return pattern.sub(block, clean).rstrip()
    return clean.rstrip() + ("\n\n" if clean else "") + block


def _attach_output(job: dict, output_file: str, duration_seconds: Optional[float] = None) -> None:
    path = Path(output_file)
    if not path.is_file():
        return
    run_at = _output_run_time(path)
    if run_at is None:
        return
    try:
        elapsed = max(float(duration_seconds or 0), 0)
    except (TypeError, ValueError):
        elapsed = 0
    run_at = run_at - timedelta(seconds=elapsed)
    job_id = str(job.get("id") or "")
    state = _load_state()
    entry = state.get(job_id, {})
    if not isinstance(entry, dict) or not _stored_events(entry):
        archived = (state.get("archived_events") or {}).get(job_id) or {}
        archived_id = archived.get("event_id") if isinstance(archived, dict) else None
        entry = {"events": [{"event_id": archived_id}]} if archived_id else {}
    stored = _stored_events(entry)
    if not stored:
        return
    event_id = stored[0].get("event_id")
    if not event_id:
        return
    tracked = state.setdefault("run_outputs", {}).setdefault(job_id, {})
    tracked_entry = tracked.get(path.name)
    calendar = _calendar_id()
    if (job.get("schedule") or {}).get("kind") == "once":
        try:
            event = calendar_client.get_event(calendar, event_id)
        except CalendarOperationError:
            return
        if not event:
            return
        existing = event.get("description") or ""
        description = _upsert_output_block(existing, path)
        if description != existing and not calendar_client.patch_event_body(
            calendar, event_id, {"description": description}
        ):
            return
        tracked[path.name] = {
            "instance_id": event_id,
            "render_version": RUN_OUTPUT_RENDER_VERSION,
            "attached_at": hermes_time.now().isoformat(),
        }
        _save_state(state)
        return
    if _is_high_frequency_schedule(job):
        tracked[path.name] = {
            "skipped": True,
            "reason": "high-frequency all-day calendar series",
            "render_version": RUN_OUTPUT_RENDER_VERSION,
        }
        _save_state(state)
        return
    instance = None
    if isinstance(tracked_entry, dict) and tracked_entry.get("instance_id"):
        try:
            instance = calendar_client.get_event(calendar, tracked_entry["instance_id"])
        except CalendarOperationError:
            return
    if instance is None:
        raw_stored_duration = entry.get("max_duration_seconds")
        try:
            stored_duration = (
                float(raw_stored_duration)
                if isinstance(raw_stored_duration, (int, float, str))
                else DEFAULT_EVENT_SECONDS
            )
        except ValueError:
            stored_duration = DEFAULT_EVENT_SECONDS
        duration = timedelta(seconds=max(elapsed or stored_duration, MIN_EVENT_SECONDS))
        try:
            instances = calendar_client.list_instances(
                calendar,
                event_id,
                (run_at - timedelta(hours=6)).isoformat(),
                (run_at + duration + timedelta(hours=6)).isoformat(),
            )
        except CalendarOperationError:
            return
        if instances:
            def distance(item: dict) -> float:
                start = item.get("start") or {}
                parsed = _parse_dt(start.get("dateTime") or start.get("date"))
                if parsed is None:
                    return float("inf")
                comparison_run_at = run_at
                if parsed.tzinfo is None and comparison_run_at.tzinfo is not None:
                    parsed = parsed.replace(tzinfo=comparison_run_at.tzinfo)
                elif parsed.tzinfo is not None and comparison_run_at.tzinfo is None:
                    comparison_run_at = comparison_run_at.replace(tzinfo=parsed.tzinfo)
                return abs((parsed - comparison_run_at).total_seconds())

            instance = min(instances, key=distance)
        elif _is_high_frequency_schedule(job):
            tracked[path.name] = {
                "skipped": True,
                "reason": "high-frequency all-day calendar series",
                "render_version": RUN_OUTPUT_RENDER_VERSION,
            }
            _save_state(state)
            return
        else:
            body = {
                "summary": _summary(job),
                "description": MANAGED_TAG + "\n\nStandalone off-schedule execution." + _render_run_output(path),
                "start": {"dateTime": run_at.isoformat(), "timeZone": _iana_timezone()},
                "end": {"dateTime": (run_at + duration).isoformat(), "timeZone": _iana_timezone()},
                "extendedProperties": {
                    "private": {
                        "managedBy": "hermes-cron-calendar-run-output",
                        "hermesCronJobId": job_id,
                        "hermesCronOutputFile": path.name,
                    }
                },
            }
            standalone_id = calendar_client.create_event_body(calendar, body)
            if standalone_id:
                tracked[path.name] = {
                    "instance_id": standalone_id,
                    "standalone": True,
                    "render_version": RUN_OUTPUT_RENDER_VERSION,
                }
                _save_state(state)
            return
    instance_id = instance.get("id") if isinstance(instance, dict) else None
    if not instance_id:
        return
    existing = instance.get("description") or ""
    description = _upsert_output_block(existing, path)
    if description != existing and not calendar_client.patch_event_body(
        calendar, instance_id, {"description": description}
    ):
        return
    tracked[path.name] = {
        "instance_id": instance_id,
        "render_version": RUN_OUTPUT_RENDER_VERSION,
        "attached_at": hermes_time.now().isoformat(),
    }
    _save_state(state)


def on_create(job: dict, **_) -> None:
    if _enabled():
        _upsert(job)


def on_update(job: dict, **_) -> None:
    if not _enabled():
        return
    if job.get("enabled") is False or job.get("state") in {"paused", "completed", "cancelled"}:
        on_remove(job)
        return
    _upsert(job)


def on_remove(job: dict, **_) -> None:
    if not _enabled():
        return
    job_id = str(job.get("id") or "")
    state = _load_state()
    entry = state.get(job_id)
    if not isinstance(entry, dict):
        return
    calendar = _calendar_id()
    event_ids = [str(item.get("event_id")) for item in _stored_events(entry) if item.get("event_id")]
    if not all(_archive_event(calendar, event_id, "cron is no longer active") for event_id in event_ids):
        return
    state.pop(job_id, None)
    archived = state.setdefault("archived_events", {})
    archived[job_id] = {
        "event_id": event_ids[0] if event_ids else "",
        "name": job.get("name"),
        "archived_at": hermes_time.now().isoformat(),
    }
    _save_state(state)


def on_complete(
    job: dict,
    success: bool,
    duration_seconds: float,
    notify=None,
    error=None,
    output_file=None,
    **_,
) -> None:
    if not _enabled():
        return
    if output_file:
        _attach_output(job, str(output_file), duration_seconds)
    if (job.get("schedule") or {}).get("kind") == "once":
        on_remove(job)
        return
    if not success or duration_seconds is None:
        return
    try:
        duration = round(float(duration_seconds), 3)
    except (TypeError, ValueError):
        return
    if duration < 0:
        return
    job_id = str(job.get("id") or "")
    state = _load_state()
    entry = state.get(job_id)
    if not isinstance(entry, dict):
        return
    prior = entry.get("max_duration_seconds")
    if prior is None:
        if not _resize_event(entry, duration):
            return
        entry["max_duration_seconds"] = duration
        entry["max_duration_updated_at"] = hermes_time.now().isoformat()
        state[job_id] = entry
        _save_state(state)
        return
    try:
        prior_value = float(prior)
    except (TypeError, ValueError):
        return
    if duration <= prior_value:
        return
    if not _resize_event(entry, duration):
        return
    entry["max_duration_seconds"] = duration
    entry["max_duration_updated_at"] = hermes_time.now().isoformat()
    state[job_id] = entry
    _save_state(state)
    if duration <= prior_value * 1.5:
        return
    name = job.get("name") or job_id
    warn = duration >= prior_value * 2
    message = (
        f"⚠️ cron took longer than expected: [{name}] increased cron max_duration "
        f"from {prior_value:.0f}s -> {duration:.0f}s"
        if warn
        else f"[{name}] increased cron max_duration from {prior_value:.0f}s -> {duration:.0f}s"
    )
    if notify is not None:
        try:
            notify(message, warn=warn)
        except Exception as exc:
            logger.error("cron_calendar_sync: notify failed for job %s: %s", job_id, exc)


def register() -> None:
    hooks.register_hook(hooks.CREATE, on_create)
    hooks.register_hook(hooks.UPDATE, on_update)
    hooks.register_hook(hooks.REMOVE, on_remove)
    hooks.register_hook(hooks.COMPLETE, on_complete)
