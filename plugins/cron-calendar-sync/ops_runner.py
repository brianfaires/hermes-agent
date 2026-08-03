#!/usr/bin/env python3
"""Sync Hermes cron jobs to Google Calendar as one recurring event per cron."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_HOME = Path("/home/brian/.hermes").resolve()
OPS_HOME = Path("/home/brian/.hermes/profiles/ops").resolve()
HERMES_HOME = OPS_HOME
PROFILE_HOMES: list[tuple[str, Path]] = [("default", DEFAULT_HOME), ("ops", OPS_HOME)]
STATE_PATH = OPS_HOME / "state" / "cron_calendar_recurring_sync.json"
CONFIG_PATH = OPS_HOME / "config.yaml"
GOOGLE_HOME = DEFAULT_HOME
GOOGLE_SCRIPT_DIR = GOOGLE_HOME / "skills" / "productivity" / "google-workspace" / "scripts"
POLICY_PATH = GOOGLE_HOME / "skills" / "productivity" / "google-workspace" / "config.json"
CALENDAR_SUMMARY = "Hermes crons"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
MANAGED_TAG = "Managed by Hermes cron_calendar_recurring_sync.py"
RUN_OUTPUT_TAG = "Hermes cron execution output"
MAX_OUTPUT_CHARS = 3500
MAX_OUTPUT_FILES_PER_JOB = 75
RUN_OUTPUT_RENDER_VERSION = 2
# The shared engine is versioned and deployed beside this runner. Do not
# resolve it through a checkout, profile, or caller-supplied source root.
RECONCILER_PATH = Path(__file__).resolve().with_name("reconciler.py")

SECRET_REDACTION_PATTERNS = [
    re.compile(r"(?i)([\"'])(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|[a-z][a-z0-9_]*(?:_secret|_token)|password|passwd|authorization|bearer)\1\s*:\s*([\"'])([^\"']{8,})\3"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|[a-z][a-z0-9_]*(?:_secret|_token)|password|passwd|authorization|bearer)\b\s*[:=]\s*([\"'])([^\"']{8,})\2"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|[a-z][a-z0-9_]*(?:_secret|_token)|password|passwd|authorization|bearer)\b\s*[:=]\s*([^\s`'\"]{8,})"),
    re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._~+/=-]{12,})"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{20,})"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{20,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{20,})"),
]

# Google Workspace credentials/policy currently live in the default profile.
# Cron-calendar ownership lives in Ops; only the Google API credential root is shared.
os.environ["HERMES_HOME"] = str(GOOGLE_HOME)

sys.path.insert(0, str(GOOGLE_SCRIPT_DIR))
try:
    import google_api  # type: ignore
except Exception as exc:
    raise SystemExit(f"Could not import google_api from {GOOGLE_SCRIPT_DIR}: {exc}")


def _load_reconciler():
    """Load the bundled engine from its sole managed source location."""
    if not RECONCILER_PATH.is_file():
        raise SystemExit(f"Calendar reconciler missing: {RECONCILER_PATH}")
    spec = importlib.util.spec_from_file_location("hermes_cron_calendar_reconciler", RECONCILER_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit("Could not load the bundled Calendar reconciler")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reconciler = _load_reconciler()

DOW = {"0": "SU", "1": "MO", "2": "TU", "3": "WE", "4": "TH", "5": "FR", "6": "SA", "7": "SU"}


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def load_config() -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise SystemExit(f"Could not parse config {CONFIG_PATH}: {exc}")
    return data if isinstance(data, dict) else {}


def calendar_sync_config() -> dict[str, Any]:
    cron_cfg = load_config().get("cron") or {}
    sync_cfg = cron_cfg.get("calendar_sync") or {}
    return sync_cfg if isinstance(sync_cfg, dict) else {}


def include_one_shots() -> bool:
    """Whether one-shot cron jobs should be mirrored as single Calendar events."""
    return bool(calendar_sync_config().get("include_one_shots", False))


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.chmod(0o600)
    tmp.replace(path)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def expand_field(field: str, minimum: int, maximum: int) -> list[int] | None:
    field = field.strip()
    if field == "*":
        return None
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, raw_step = part.split("/", 1)
            step = int(raw_step)
        else:
            base = part
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            raw_start, raw_end = base.split("-", 1)
            start, end = int(raw_start), int(raw_end)
        else:
            start = end = int(base)
        for value in range(start, end + 1, step):
            if minimum <= value <= maximum:
                values.add(value)
    return sorted(values)


def cron_to_rrule(expr: str) -> tuple[str, int, int]:
    """Return (RRULE, start_hour, start_minute) for supported Hermes cron expressions."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"unsupported cron expression: {expr}")
    minute_f, hour_f, dom_f, month_f, dow_f = parts
    minutes = expand_field(minute_f, 0, 59)
    hours = expand_field(hour_f, 0, 23)
    doms = expand_field(dom_f, 1, 31)
    months = expand_field(month_f, 1, 12)
    dows = expand_field(dow_f, 0, 7)

    start_minute = (minutes or [0])[0]
    start_hour = (hours or [0])[0]
    bits: list[str]

    if doms is not None and dows is not None:
        raise ValueError(f"unsupported cron with both day-of-month and day-of-week: {expr}")

    if doms is not None:
        bits = ["FREQ=MONTHLY", "BYMONTHDAY=" + ",".join(map(str, doms))]
    elif dows is not None:
        byday = ",".join(DOW[str(d)] for d in dows)
        bits = ["FREQ=WEEKLY", f"BYDAY={byday}"]
    else:
        bits = ["FREQ=DAILY"]

    if months is not None:
        bits.append("BYMONTH=" + ",".join(map(str, months)))
    if hours is not None and len(hours) > 1:
        bits.append("BYHOUR=" + ",".join(map(str, hours)))
    if minutes is not None and len(minutes) > 1:
        bits.append("BYMINUTE=" + ",".join(map(str, minutes)))
    elif minutes is not None:
        bits.append(f"BYMINUTE={minutes[0]}")
    elif hours is not None and len(hours) > 1:
        bits.append(f"BYMINUTE={start_minute}")

    return "RRULE:" + ";".join(bits), start_hour, start_minute


def schedule_interval_seconds(job: dict[str, Any]) -> int | None:
    """Best-effort recurrence interval for active cron/interval schedules."""
    schedule = job.get("schedule") or {}
    kind = schedule.get("kind")
    if kind == "interval":
        for key, multiplier in (("seconds", 1), ("minutes", 60), ("hours", 3600)):
            value = schedule.get(key)
            if value is not None:
                try:
                    seconds = int(value) * multiplier
                except (TypeError, ValueError):
                    continue
                return seconds if seconds > 0 else None
        display = str(schedule.get("display") or job.get("schedule_display") or "")
        match = re.search(r"every\s+(\d+)\s*(s|sec|second|seconds|m|min|minute|minutes|h|hr|hour|hours)\b", display, re.IGNORECASE)
        if match:
            value = int(match.group(1))
            unit = match.group(2).lower()
            if unit.startswith("s"):
                return value
            if unit.startswith("m"):
                return value * 60
            return value * 3600
        return None
    if kind != "cron":
        return None
    expr = schedule.get("expr") or job.get("schedule_display")
    if not expr:
        return None
    parts = str(expr).split()
    if len(parts) != 5:
        return None
    minute_f, hour_f, dom_f, month_f, dow_f = parts
    try:
        minutes = expand_field(minute_f, 0, 59)
        hours = expand_field(hour_f, 0, 23)
    except Exception:
        return None
    # Any wildcard minute/hour field means intra-day recurrence well under 6h.
    if minutes is None:
        return 60
    if hours is None:
        if len(minutes) > 1:
            gaps = [b - a for a, b in zip(minutes, minutes[1:])]
            gaps.append((60 - minutes[-1]) + minutes[0])
            return min(gaps) * 60
        return 3600
    # Multiple times in a day: compute nearest gap among daily fire times.
    fire_minutes = sorted({hour * 60 + minute for hour in hours for minute in minutes})
    if len(fire_minutes) > 1:
        gaps = [b - a for a, b in zip(fire_minutes, fire_minutes[1:])]
        gaps.append((24 * 60 - fire_minutes[-1]) + fire_minutes[0])
        return min(gaps) * 60
    # Single fire time: daily cron is 24h; weekly/monthly are longer.
    if dom_f == "*" and dow_f == "*" and month_f == "*":
        return 24 * 3600
    return None


def is_high_frequency_schedule(job: dict[str, Any]) -> bool:
    interval = schedule_interval_seconds(job)
    return interval is not None and interval <= 6 * 3600


def estimate_minutes(job: dict[str, Any]) -> int:
    """Return calendar duration from canonical Ops reconciliation state."""
    try:
        max_duration = job.get("max_duration_seconds")
        if max_duration is not None:
            seconds = float(max_duration)
            if seconds > 0:
                return max(1, int(math.ceil(seconds / 60)))
    except (TypeError, ValueError):
        pass

    # Unknown should mean "small unknown", not fake hour-long blocks. Most cron
    # scripts here are watchdogs/backups that complete fast; real measurements
    # replace this as soon as the duration tracker observes a run.
    return 5


def profile_home(job: dict[str, Any]) -> Path:
    profile = job.get("__profile", "default")
    for name, home in PROFILE_HOMES:
        if profile == name:
            return home
    raise ValueError(f"unmanaged cron profile: {profile}")


def validated_job_id(job: dict[str, Any]) -> str:
    """Return one safe cron identifier component or reject the snapshot."""
    value = job.get("id")
    if not isinstance(value, str):
        raise ValueError("invalid cron job id")
    job_id = value.strip()
    if (
        not job_id
        or job_id != value
        or job_id in {".", ".."}
        or "/" in job_id
        or "\\" in job_id
    ):
        raise ValueError("invalid cron job id")
    return job_id


def output_root(job: dict[str, Any]) -> Path:
    return profile_home(job) / "cron" / "output"


def runtime_state_path(job: dict[str, Any]) -> Path:
    return profile_home(job) / "cron" / "calendar_sync.json"


def latest_output_file(job: dict[str, Any]) -> Path | None:
    directory = output_root(job) / str(job.get("id"))
    if not directory.exists():
        return None
    files = sorted(directory.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def summarize_output(job: dict[str, Any]) -> str:
    status = job.get("last_status") or "not yet run"
    if job.get("last_error"):
        return f"Last status: {status}. Error: {str(job.get('last_error'))[:500]}"
    output_file = latest_output_file(job)
    if not output_file:
        return f"Last status: {status}. No cron output file found yet."
    text = output_file.read_text(errors="replace")
    if "**Status:** silent (empty output)" in text:
        return f"Last status: {status}. Script completed silently with empty output."
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("**") or line.startswith("[IMPORTANT:"):
            continue
        line = re.sub(r"\s+", " ", line)
        lines.append(line)
        if len(" ".join(lines)) > 500:
            break
    snippet = " ".join(lines).strip() or f"Output recorded in {output_file.name}."
    if len(snippet) > 700:
        snippet = snippet[:697].rstrip() + "…"
    return f"Last status: {status}. {snippet}"


def output_files(job: dict[str, Any]) -> list[Path]:
    directory = output_root(job) / str(job.get("id"))
    if not directory.exists():
        return []
    files = sorted(directory.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:MAX_OUTPUT_FILES_PER_JOB]


def all_output_files(job: dict[str, Any]) -> list[Path]:
    directory = output_root(job) / str(job.get("id"))
    if not directory.exists():
        return []
    return sorted(directory.glob("*.md"), key=lambda p: p.stat().st_mtime)


def output_run_time(path: Path) -> datetime | None:
    match = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})\.md$", path.name)
    if not match:
        return None
    date, hour, minute, second = match.groups()
    try:
        return datetime.fromisoformat(f"{date}T{hour}:{minute}:{second}").replace(tzinfo=LOCAL_TZ)
    except ValueError:
        return None


def render_run_output(path: Path) -> str:
    text = extract_final_output(path.read_text(errors="replace")).strip()
    text = redact_secrets(text)
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[: MAX_OUTPUT_CHARS - 1].rstrip() + "…"
    return f"\n\n---\n{RUN_OUTPUT_TAG}: {path.name}\nRender version: {RUN_OUTPUT_RENDER_VERSION}\n\n{text}"


def redact_secrets(text: str) -> str:
    """Best-effort redaction before cron output is copied into Calendar."""
    redacted = text
    for pattern in SECRET_REDACTION_PATTERNS:
        def repl(match: re.Match[str]) -> str:
            if len(match.groups()) == 4:
                return f"{match.group(1)}{match.group(2)}{match.group(1)}: {match.group(3)}REDACTED{match.group(3)}"
            if len(match.groups()) == 3:
                return f"{match.group(1)}: {match.group(2)}REDACTED{match.group(2)}"
            if len(match.groups()) >= 2:
                return f"{match.group(1)}: REDACTED"
            return "REDACTED"
        redacted = pattern.sub(repl, redacted)
    return redacted


def extract_final_output(saved_output: str) -> str:
    """Return only the cron run's final response/error, never the prompt.

    Agent-mode cron output files include both ``## Prompt`` and ``## Response``
    for local audit. Calendar is a user-visible surface, so it must receive the
    final result only. Script/no-agent jobs do not have prompt sections; when a
    ``---`` body separator is present, return only the script stdout after it.
    """
    text = saved_output.replace("\r\n", "\n").strip()
    response_match = re.search(r"(?ms)^## Response\s*\n(?P<body>.*)$", text)
    if response_match:
        return response_match.group("body").strip() or "(No response generated)"

    error_match = re.search(r"(?ms)^## Error\s*\n(?P<body>.*)$", text)
    if error_match:
        return "Cron failed:\n" + error_match.group("body").strip()

    # Blocked runs and silent no-agent/script gates have status metadata but no
    # response body. Keep the operator-visible status, not the full local doc.
    status_match = re.search(r"(?mi)^\*\*Status:\*\*\s*(.+)$", text)
    if status_match and "\n---\n" not in text:
        return f"Cron status: {status_match.group(1).strip()}"

    if "\n---\n" in text:
        return text.split("\n---\n", 1)[1].strip()

    return strip_prompt_sections(text).strip()


def strip_prompt_sections(description_text: str) -> str:
    """Remove prompt-bearing sections from inherited/legacy descriptions."""
    text = description_text.replace("\r\n", "\n")
    # Remove the old recurring-series prompt preview block and any local-output
    # prompt section that was previously copied into an instance description.
    text = re.sub(r"(?ms)\n?Prompt preview:\n.*?(?=\n\n---\n|\Z)", "", text)
    text = re.sub(r"(?ms)\n?## Prompt\n.*?(?=\n## (?:Response|Error)\n|\Z)", "", text)
    return redact_secrets(text).rstrip()


def upsert_run_output_block(existing: str, output_file: Path) -> str:
    """Insert or replace the Calendar block for one cron output file."""
    clean_existing = strip_prompt_sections(existing)
    block = render_run_output(output_file).lstrip("\n")
    marker = re.escape(f"{RUN_OUTPUT_TAG}: {output_file.name}")
    pattern = re.compile(
        rf"(?ms)(?:\n\n)?---\n{marker}\n(?:Render version: \d+\n)?\n.*?(?=(?:\n\n---\n{re.escape(RUN_OUTPUT_TAG)}: )|\Z)"
    )
    if pattern.search(clean_existing):
        return pattern.sub(block, clean_existing).rstrip()
    if clean_existing:
        return clean_existing.rstrip() + "\n\n" + block
    return block



def earliest_output_run_time(job: dict[str, Any]) -> datetime | None:
    for path in all_output_files(job):
        run_at = output_run_time(path)
        if run_at:
            return run_at
    return None


def utc_until_before(now: datetime | None = None) -> str:
    dt = (now or datetime.now(LOCAL_TZ)).astimezone(timezone.utc) - timedelta(seconds=1)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def truncate_rrule_at_now(rrule: str) -> str:
    if not rrule.startswith("RRULE:"):
        return rrule
    body = rrule[len("RRULE:"):]
    parts = [part for part in body.split(";") if part and not part.startswith("UNTIL=") and not part.startswith("COUNT=")]
    parts.append(f"UNTIL={utc_until_before()}")
    return "RRULE:" + ";".join(parts)


def attach_output_file_to_instance(
    service,
    calendar_id: str,
    event_id: str,
    job: dict[str, Any],
    state: dict[str, Any],
    output_file: Path,
    dry_run: bool,
) -> tuple[int, list[str]]:
    job_id = str(job.get("id"))
    tracked = state.setdefault("run_outputs", {}).setdefault(job_id, {})
    messages: list[str] = []
    tracked_entry = tracked.get(output_file.name)
    if isinstance(tracked_entry, dict) and tracked_entry.get("render_version") == RUN_OUTPUT_RENDER_VERSION:
        return 0, messages
    run_at = output_run_time(output_file)
    if not run_at:
        return 0, messages
    if dry_run:
        return 1, messages
    if isinstance(tracked_entry, dict) and tracked_entry.get("instance_id"):
        try:
            instance_id = tracked_entry["instance_id"]
            instance = service.events().get(calendarId=calendar_id, eventId=instance_id).execute()
            existing = instance.get("description") or ""
            new_description = upsert_run_output_block(existing, output_file)
            if new_description != existing:
                service.events().patch(calendarId=calendar_id, eventId=instance_id, body={"description": new_description}).execute()
            tracked[output_file.name].update({"attached_at": datetime.now(LOCAL_TZ).isoformat(), "render_version": RUN_OUTPUT_RENDER_VERSION})
            return 1, messages
        except Exception as exc:
            messages.append(f"{job_id} {output_file.name}: existing tracked instance refresh failed: {exc}")
    duration = timedelta(minutes=estimate_minutes(job))
    time_min = (run_at - timedelta(hours=6)).isoformat()
    time_max = (run_at + duration + timedelta(hours=6)).isoformat()
    try:
        instances = service.events().instances(calendarId=calendar_id, eventId=event_id, timeMin=time_min, timeMax=time_max).execute().get("items", [])
        if not instances:
            if is_high_frequency_schedule(job):
                tracked[output_file.name] = {"skipped": True, "reason": "high-frequency all-day calendar series", "attached_at": datetime.now(LOCAL_TZ).isoformat()}
                return 0, messages
            run_event = {
                "summary": normalize_event_summary(job.get("name"), job.get("script")),
                "description": f"{MANAGED_TAG}\n\nStandalone cron execution event for an off-schedule or no-longer-matching run." + render_run_output(output_file),
                "start": {"dateTime": run_at.isoformat(), "timeZone": str(LOCAL_TZ)},
                "end": {"dateTime": (run_at + duration).isoformat(), "timeZone": str(LOCAL_TZ)},
                "extendedProperties": {"private": {"managedBy": "hermes-cron-calendar-run-output", "hermesCronJobId": job_id, "hermesCronOutputFile": output_file.name}},
            }
            created = service.events().insert(calendarId=calendar_id, body=run_event).execute()
            tracked[output_file.name] = {"instance_id": created.get("id"), "attached_at": datetime.now(LOCAL_TZ).isoformat(), "standalone": True, "render_version": RUN_OUTPUT_RENDER_VERSION}
            return 1, messages
        def distance(item: dict[str, Any]) -> float:
            start = item.get("start") or {}
            raw = start.get("dateTime") or start.get("date")
            dt = parse_dt(raw)
            return abs((dt - run_at).total_seconds()) if dt else float("inf")
        instance = min(instances, key=distance)
        instance_id = instance.get("id")
        if not instance_id:
            messages.append(f"{job_id} {output_file.name}: matched instance has no id")
            return 0, messages
        existing = instance.get("description") or ""
        new_description = upsert_run_output_block(existing, output_file)
        if new_description != existing:
            service.events().patch(calendarId=calendar_id, eventId=instance_id, body={"description": new_description}).execute()
        tracked[output_file.name] = {"instance_id": instance_id, "attached_at": datetime.now(LOCAL_TZ).isoformat(), "render_version": RUN_OUTPUT_RENDER_VERSION}
        return 1, messages
    except Exception as exc:
        messages.append(f"{job_id} {output_file.name}: {exc}")
        return 0, messages


def attach_outputs_to_instances(service, calendar_id: str, event_id: str, job: dict[str, Any], state: dict[str, Any], dry_run: bool) -> tuple[int, list[str]]:
    job_id = str(job.get("id"))
    attached = 0
    messages: list[str] = []
    for output_file in reversed(output_files(job)):
        count, file_messages = attach_output_file_to_instance(service, calendar_id, event_id, job, state, output_file, dry_run)
        attached += count
        messages.extend(file_messages)
    return attached, messages


def attach_output_to_calendar_event(job: dict[str, Any], output_file: str | Path, dry_run: bool = False) -> dict[str, Any]:
    """Compatibility entrypoint routed through the canonical single-job path."""
    return reconcile_single_job(
        job,
        "complete",
        output_file=str(output_file),
        success=bool(job.get("last_status") == "success"),
        duration_seconds=None,
        dry_run=dry_run,
    )


def prompt_preview(job: dict[str, Any], limit: int = 450) -> str:
    text = (job.get("prompt") or "").strip().replace("\r\n", "\n")
    return text[: limit - 1].rstrip() + "…" if len(text) > limit else text


def description(job: dict[str, Any], rrule: str | None, *, all_day: bool = False, one_shot: bool = False) -> str:
    repeat = job.get("repeat") or {}
    repeat_limit = repeat.get("times") or "forever"
    lines = [
        MANAGED_TAG,
        "",
        f"Cron job: {job.get('name')}",
        f"Job ID: {job.get('id')}",
        f"Profile: {job.get('__profile', 'default')}",
        f"Schedule: {job.get('schedule_display') or job.get('schedule', {}).get('display')}",
    ]
    if one_shot:
        lines.append("Calendar representation: one-shot cron event")
    else:
        lines.append(f"Google recurrence: {rrule}")
    if all_day:
        lines.append("Calendar representation: high-frequency schedule (<=6h) collapsed to one all-day event recurring every 24 hours")
    else:
        lines.append(f"Measured runtime: {estimate_minutes(job)} minutes (max observed when available; 5-minute placeholder until measured)")
    lines.extend([
        f"Script: {job.get('script') or '(agent prompt)'}",
        f"Delivery: {job.get('deliver')}",
        f"Repeat limit: {repeat_limit}",
    ])
    if job.get("prompt") or job.get("prompt_path"):
        lines.append("Prompt: configured, intentionally not shown in Calendar")
    return "\n".join(lines)


def ensure_policy_seen() -> None:
    if not POLICY_PATH.exists():
        raise SystemExit(f"Google Workspace policy missing: {POLICY_PATH}")
    json.loads(POLICY_PATH.read_text())


def get_service():
    ensure_policy_seen()
    return google_api.build_service("calendar", "v3")


def find_calendar(service) -> str | None:
    page_token = None
    while True:
        result = service.calendarList().list(pageToken=page_token).execute()
        for item in result.get("items", []):
            if item.get("summary") == CALENDAR_SUMMARY:
                return item.get("id")
        page_token = result.get("nextPageToken")
        if not page_token:
            return None


def ensure_calendar(service, dry_run: bool) -> str:
    existing = find_calendar(service)
    if existing:
        return existing
    if dry_run:
        return "dry-run-calendar-id"
    created = service.calendars().insert(body={"summary": CALENDAR_SUMMARY, "timeZone": str(LOCAL_TZ)}).execute()
    return created["id"]


def should_include(job: dict[str, Any]) -> bool:
    schedule = job.get("schedule") or {}
    kinds = {"cron", "interval"}
    if include_one_shots():
        kinds.add("once")
    return bool(job.get("enabled")) and job.get("state") == "scheduled" and schedule.get("kind") in kinds


def default_emoji_for_job(name: str | None, script: str | None = None) -> str:
    text = f"{name or ''} {script or ''}".lower()
    if "morning" in text or "brief" in text:
        return "☀️"
    if "hindsight" in text:
        return "🧠"
    if "langfuse" in text or "langfush" in text:
        return "📐"
    if "backup" in text or "drive" in text or "config" in text:
        return "💾"
    if "health" in text:
        return "🩺"
    if "cleanup" in text:
        return "🧹"
    if "skills" in text or "curator" in text:
        return "🛠️"
    if "news" in text or "monitor" in text or "groq" in text:
        return "🛰️"
    if "trash" in text:
        return "🗑️"
    if "posture" in text or "reminder" in text:
        return "⏰"
    return "⏰"


def normalize_event_summary(name: str | None, script: str | None = None) -> str:
    text = (name or "").strip()
    text = re.sub(r"^Hermes\s+", "", text)
    text = re.sub(r"^Reminder\s+[0-9a-f]+:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^Hermes cron:\s*", "", text)
    text = text or "(unnamed cron)"
    if re.match(r"^[^\w\s]\ufe0f?\s+", text):
        return text
    return f"{default_emoji_for_job(text, script)} {text}"


def event_for_job(job: dict[str, Any]) -> dict[str, Any]:
    base_start = parse_dt(job.get("created_at")) or parse_dt(job.get("next_run_at")) or datetime.now(LOCAL_TZ)
    schedule = job.get("schedule") or {}
    if schedule.get("kind") == "once":
        start = parse_dt(schedule.get("run_at")) or parse_dt(job.get("next_run_at")) or base_start
        end = start + timedelta(minutes=estimate_minutes(job))
        return {
            "summary": normalize_event_summary(job.get('name'), job.get('script')),
            "description": description(job, None, one_shot=True),
            "start": {"dateTime": start.isoformat(), "timeZone": str(LOCAL_TZ)},
            "end": {"dateTime": end.isoformat(), "timeZone": str(LOCAL_TZ)},
            "extendedProperties": {"private": {"managedBy": "hermes-cron-calendar-recurring-sync", "hermesCronJobId": str(job.get("id")), "hermesCronProfile": str(job.get("__profile", "default")), "hermesCronCalendarMode": "one-shot"}},
        }
    all_day = is_high_frequency_schedule(job)
    if all_day:
        rrule = "RRULE:FREQ=DAILY;INTERVAL=1"
        start_date = base_start.astimezone(LOCAL_TZ).date()
        body = {
            "summary": normalize_event_summary(job.get('name'), job.get('script')),
            "description": description(job, rrule, all_day=True),
            "start": {"date": start_date.isoformat()},
            "end": {"date": (start_date + timedelta(days=1)).isoformat()},
            "recurrence": [rrule],
            "extendedProperties": {"private": {"managedBy": "hermes-cron-calendar-recurring-sync", "hermesCronJobId": str(job.get("id")), "hermesCronProfile": str(job.get("__profile", "default")), "hermesCronCalendarMode": "all-day-high-frequency"}},
        }
        return body

    expr = job.get("schedule", {}).get("expr") or job.get("schedule_display")
    if not expr:
        raise ValueError("missing cron expression")
    rrule, start_hour, start_minute = cron_to_rrule(expr)
    start = base_start.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end = start + timedelta(minutes=estimate_minutes(job))
    body = {
        "summary": normalize_event_summary(job.get('name'), job.get('script')),
        "description": description(job, rrule),
        "start": {"dateTime": start.isoformat(), "timeZone": str(LOCAL_TZ)},
        "end": {"dateTime": end.isoformat(), "timeZone": str(LOCAL_TZ)},
        "recurrence": [rrule],
        "extendedProperties": {"private": {"managedBy": "hermes-cron-calendar-recurring-sync", "hermesCronJobId": str(job.get("id")), "hermesCronProfile": str(job.get("__profile", "default"))}},
    }
    return body


def signature(body: dict[str, Any]) -> str:
    # Calendar summaries are Brian-editable UI labels. They must never drive
    # reconciliation, otherwise manual emoji/title edits become accidental drift.
    relevant = {k: body.get(k) for k in ["description", "start", "end", "recurrence"]}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()


def calendar_identity(job: dict[str, Any]) -> tuple[Any, ...]:
    """Fields that must match before cross-profile duplicate IDs can be coalesced."""
    schedule = job.get("schedule") or {}
    repeat = job.get("repeat") or {}
    return (
        job.get("id"),
        job.get("name"),
        job.get("enabled"),
        job.get("state"),
        schedule.get("kind"),
        schedule.get("expr"),
        schedule.get("display"),
        job.get("schedule_display"),
        job.get("script"),
        job.get("deliver"),
        repeat.get("times"),
    )


def prefer_duplicate_job(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Prefer the Ops-owned record for identical compatibility duplicates."""
    if existing.get("__profile") != candidate.get("__profile"):
        if candidate.get("__profile") == "ops":
            return candidate
        if existing.get("__profile") == "ops":
            return existing
    return existing


def load_all_jobs() -> list[dict[str, Any]]:
    jobs_by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for profile, home in PROFILE_HOMES:
        jobs_path = home / "cron" / "jobs.json"
        payload = load_json(jobs_path, {"jobs": []})
        raw_jobs = payload.get("jobs", [])
        if not isinstance(raw_jobs, list):
            raise SystemExit(f"Invalid cron jobs file: {jobs_path}")
        for raw in raw_jobs:
            if not isinstance(raw, dict):
                continue
            job = dict(raw)
            job_id = str(job.get("id"))
            job["__profile"] = profile
            job["__profile_home"] = str(home)
            existing = jobs_by_id.get(job_id)
            if existing is not None:
                if calendar_identity(existing) != calendar_identity(job):
                    raise SystemExit(f"Conflicting duplicate cron job id across calendar-managed profiles: {job_id}")
                jobs_by_id[job_id] = prefer_duplicate_job(existing, job)
                continue
            jobs_by_id[job_id] = job
            order.append(job_id)
    return [jobs_by_id[job_id] for job_id in order]


def live_events_by_job(service, calendar_id: str) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    page_token = None
    while True:
        response = service.events().list(calendarId=calendar_id, singleEvents=False, showDeleted=False, maxResults=2500, pageToken=page_token).execute()
        for item in response.get("items", []):
            desc = item.get("description") or ""
            private = (item.get("extendedProperties") or {}).get("private") or {}
            job_id = private.get("hermesCronJobId")
            if not job_id:
                match = re.search(r"(?:Job ID:|cron job_id:)\s*([0-9a-f]{12})", desc)
                job_id = match.group(1) if match else None
            if job_id:
                result.setdefault(str(job_id), []).append(item)
        page_token = response.get("nextPageToken")
        if not page_token:
            return result


def adoptable_event_id(live_by_job: dict[str, list[dict[str, Any]]], job_id: str, tracked_event_ids: set[str]) -> str | None:
    event_ids = untracked_live_event_ids(live_by_job, job_id, tracked_event_ids)
    return event_ids[0] if event_ids else None


def untracked_live_event_ids(live_by_job: dict[str, list[dict[str, Any]]], job_id: str, tracked_event_ids: set[str]) -> list[str]:
    event_ids = {
        str(item.get("id") or "")
        for item in live_by_job.get(job_id, [])
        if item.get("id") and str(item.get("id")) not in tracked_event_ids and item.get("status") != "cancelled"
    }
    return sorted(event_ids)


def _managed_output_file(value: str | None, job: dict[str, Any]) -> Path | None:
    if not value or not isinstance(value, str):
        return None
    try:
        path = Path(value).resolve(strict=True)
        job_id = validated_job_id(job)
        if path.suffix != ".md":
            return None
        path.relative_to(output_root(job) / job_id)
        return path
    except (OSError, ValueError):
        return None


def _event_job(job: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    value = dict(job)
    entry = (state.get("events") or {}).get(str(job.get("id")))
    if isinstance(entry, dict) and entry.get("max_duration_seconds") is not None:
        value["max_duration_seconds"] = entry["max_duration_seconds"]
    return value


def _archive_event(service, calendar_id: str, event_id: str, reason: str) -> object:
    event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    recurrence = event.get("recurrence") or []
    patch_body: dict[str, Any] = {}
    if recurrence:
        patch_body["recurrence"] = [truncate_rrule_at_now(recurrence[0])]
    description_text = event.get("description") or ""
    if "Archived by Hermes cron calendar sync" not in description_text:
        patch_body["description"] = (
            description_text
            + f"\n\n---\nArchived by Hermes cron calendar sync: {reason}. Past instances are intentionally retained."
        )
    return service.events().patch(calendarId=calendar_id, eventId=event_id, body=patch_body).execute() if patch_body else True


def _build_context(service, calendar_id: str, dry_run: bool, live_by_job: dict[str, list[dict[str, Any]]]):
    holder: dict[str, Any] = {}

    def load_state() -> dict[str, Any]:
        state = load_json(STATE_PATH, {"events": {}})
        if not isinstance(state, dict):
            raise ValueError("canonical Calendar state must be an object")
        state.setdefault("events", {})
        return state

    def save_state(state: dict[str, Any]) -> None:
        state.update({"calendar_summary": CALENDAR_SUMMARY, "calendar_id": calendar_id, "last_sync_at": datetime.now(LOCAL_TZ).isoformat()})
        save_json(STATE_PATH, state)

    def attach_output(output_file: str, job: dict[str, Any]) -> int:
        path = _managed_output_file(output_file, job)
        if path is None:
            raise ValueError("unmanaged cron output path")
        context = holder["context"]
        entry = ((context.state.get("events") or {}).get(str(job.get("id"))) or {})
        event_id = entry.get("event_id")
        if not event_id:
            return 0
        if (job.get("schedule") or {}).get("kind") == "once":
            event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
            description_text = event.get("description") or ""
            updated = upsert_run_output_block(description_text, path)
            if updated != description_text:
                service.events().patch(calendarId=calendar_id, eventId=event_id, body={"description": updated}).execute()
            context.state.setdefault("run_outputs", {}).setdefault(str(job.get("id")), {})[path.name] = {"instance_id": event_id, "attached_at": datetime.now(LOCAL_TZ).isoformat(), "render_version": RUN_OUTPUT_RENDER_VERSION}
            return 1
        attached, messages = attach_output_file_to_instance(service, calendar_id, event_id, job, context.state, path, dry_run)
        if messages:
            raise RuntimeError("; ".join(messages))
        return attached

    def record_completion(job: dict[str, Any], success: bool, duration_seconds: float | None) -> None:
        if not success or duration_seconds is None:
            return
        try:
            duration = float(duration_seconds)
        except (TypeError, ValueError):
            return
        if duration < 0:
            return
        context = holder["context"]
        entry = ((context.state.get("events") or {}).get(str(job.get("id"))) or {})
        event_id = entry.get("event_id")
        prior = entry.get("max_duration_seconds")
        try:
            if prior is not None and duration <= float(prior):
                return
        except (TypeError, ValueError):
            pass
        if not event_id:
            return
        measured = dict(job)
        measured["max_duration_seconds"] = duration
        body = event_for_job(measured)
        patch_body = dict(body)
        patch_body.pop("summary", None)
        service.events().patch(calendarId=calendar_id, eventId=event_id, body=patch_body).execute()
        entry.update({"max_duration_seconds": duration, "max_duration_updated_at": datetime.now(LOCAL_TZ).isoformat(), "signature": signature(body)})
        context.state.setdefault("events", {})[str(job.get("id"))] = entry

    context = reconciler.ReconcileContext(
        state={"events": {}},
        dry_run=dry_run,
        calendar_id=calendar_id,
        should_include=should_include,
        event_for_job=lambda job: event_for_job(_event_job(job, holder["context"].state)),
        signature=signature,
        get_event=lambda event_id: service.events().get(calendarId=calendar_id, eventId=event_id).execute(),
        create_event=lambda body: service.events().insert(calendarId=calendar_id, body=body).execute().get("id"),
        patch_event=lambda event_id, body: service.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute(),
        adopt_event_id=lambda job_id, tracked: adoptable_event_id(live_by_job, job_id, tracked),
        untracked_event_ids=lambda job_id, tracked: untracked_live_event_ids(live_by_job, job_id, tracked),
        archive_event=lambda event_id, reason: _archive_event(service, calendar_id, event_id, reason),
        attach_output=attach_output,
        record_completion=record_completion,
        now=lambda: datetime.now(LOCAL_TZ).isoformat(),
        load_state=load_state,
        save_state=save_state,
        lock=lambda: reconciler.CalendarStateLock(STATE_PATH.with_suffix(".lock")),
    )
    holder["context"] = context
    return context


def reconcile_single_job(job: dict[str, Any], operation: str, *, output_file: str | None = None, success: bool = False, duration_seconds: float | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Reconcile exactly one lifecycle snapshot without enumerating cron inventory."""
    if not isinstance(job, dict):
        raise ValueError("single-job payload requires a job object")
    snapshot = dict(job)
    validated_job_id(snapshot)
    snapshot.pop("__profile_home", None)
    profile = snapshot.get("__profile", "default")
    if profile not in {"default", "ops"}:
        raise ValueError("single-job payload references an unmanaged profile")
    snapshot["__profile"] = profile
    service = get_service()
    calendar_id = ensure_calendar(service, dry_run)
    context = _build_context(service, calendar_id, dry_run, {})
    return reconciler.reconcile_one(snapshot, operation, context, output_file=output_file, success=success, duration_seconds=duration_seconds)


def sync(dry_run: bool = False, skip_output_attachments: bool = False) -> dict[str, Any]:
    """Recovery sweep: inventory once, reconcile each canonical job once, then sweep orphans."""
    jobs = load_all_jobs()
    service = get_service()
    calendar_id = ensure_calendar(service, dry_run)
    context = _build_context(service, calendar_id, dry_run, {} if dry_run else live_events_by_job(service, calendar_id))
    totals: dict[str, Any] = {"calendar_id": calendar_id, "errors": 0, "error_messages": [], "dry_run": dry_run, "skipped_output_attachments": skip_output_attachments}
    for job in jobs:
        try:
            result = reconciler.reconcile_one(job, "update", context)
            for key, value in result.items():
                if isinstance(value, int):
                    totals[key] = totals.get(key, 0) + value
        except Exception as exc:
            totals["errors"] += 1
            totals["error_messages"].append(
                redact_secrets(f"{job.get('id')} {job.get('name')}: {exc}")
            )
    try:
        orphan_result = reconciler.reconcile_orphans(jobs, context)
        for key, value in orphan_result.items():
            totals[f"orphan_{key}"] = value
    except Exception as exc:
        totals["errors"] += 1
        totals["error_messages"].append(redact_secrets(f"orphan sweep: {exc}"))
    totals["error_messages"] = totals["error_messages"][:50]
    return totals


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-output-attachments",
        action="store_true",
        help="Reconcile cron series/state without replaying historical run-output attachments.",
    )
    parser.add_argument("--single-job", action="store_true", help="Read one lifecycle snapshot from stdin; never enumerate cron inventory.")
    args = parser.parse_args()
    if args.single_job:
        try:
            payload = json.loads(sys.stdin.read())
            if not isinstance(payload, dict):
                raise ValueError("single-job payload must be an object")
            raw_job = payload.get("job")
            if not isinstance(raw_job, dict):
                raise ValueError("single-job payload requires a job object")
            result = reconcile_single_job(
                raw_job,
                str(payload.get("operation") or ""),
                output_file=payload.get("output_file"),
                success=bool(payload.get("success")),
                duration_seconds=payload.get("duration_seconds"),
                dry_run=args.dry_run,
            )
        except Exception as exc:
            result = {
                "errors": 1,
                "error_messages": [redact_secrets(str(exc))],
                "dry_run": args.dry_run,
            }
    else:
        result = sync(dry_run=args.dry_run, skip_output_attachments=args.skip_output_attachments)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result.get("errors", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
