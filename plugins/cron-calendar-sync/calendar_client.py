"""Thin adapter over the google-workspace skill's calendar CLI.

cron_calendar_sync uses this to create/update/delete Google Calendar events for
cron jobs. All calls route through the installed google-workspace skill
(``google_api.py``) so the skill's fail-closed write policy and OAuth handling
stay the single source of truth — this module adds no Google API code of its
own.

Every method is best-effort: failures are logged and surfaced as ``None`` /
``False`` rather than raised, so a calendar problem never breaks cron job
mutations or scheduler runs.
"""

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def _script_path() -> Path:
    return (
        get_hermes_home()
        / "skills"
        / "productivity"
        / "google-workspace"
        / "scripts"
        / "google_api.py"
    )


def _token_path() -> Path:
    return get_hermes_home() / "google-workspace" / "google_token.json"


def available() -> bool:
    """True when the skill script and an OAuth token are both present."""
    return _script_path().exists() and _token_path().exists()


def _parse_json_stdout(stdout: str) -> Optional[dict]:
    """Extract the first JSON object from CLI stdout (ignoring stray noise)."""
    if not stdout:
        return None
    start = stdout.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(stdout)):
        ch = stdout[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(stdout[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _run(args: list) -> Optional[dict]:
    """Invoke ``google_api.py calendar <args>`` and return parsed JSON output."""
    script = _script_path()
    if not script.exists():
        logger.warning("cron_calendar_sync: google-workspace script missing at %s", script)
        return None
    cmd = [sys.executable, str(script), "calendar", *[str(a) for a in args]]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL
        )
    except Exception as e:
        logger.error("cron_calendar_sync: calendar command failed to run: %s", e)
        return None
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        logger.warning("cron_calendar_sync: calendar %s failed: %s", args[0] if args else "?", err)
        return None
    return _parse_json_stdout(result.stdout)


def create_event(calendar: str, summary: str, start: str, end: str,
                 *, recurrence: Optional[str] = None, timezone: Optional[str] = None,
                 description: Optional[str] = None) -> Optional[str]:
    """Create an event; return its event id or None on failure."""
    args = ["create", "--calendar", calendar, "--summary", summary,
            "--start", start, "--end", end]
    if recurrence:
        args += ["--recurrence", recurrence]
    if timezone:
        args += ["--timezone", timezone]
    if description:
        args += ["--description", description]
    result = _run(args)
    return result.get("id") if result else None


def update_event(calendar: str, event_id: str, *, summary: Optional[str] = None,
                 start: Optional[str] = None, end: Optional[str] = None,
                 recurrence: Optional[str] = None, timezone: Optional[str] = None,
                 description: Optional[str] = None) -> bool:
    """Patch an existing event. Return True on success."""
    args = ["update", str(event_id), "--calendar", calendar]
    if summary:
        args += ["--summary", summary]
    if start:
        args += ["--start", start]
    if end:
        args += ["--end", end]
    if recurrence:
        args += ["--recurrence", recurrence]
    if timezone:
        args += ["--timezone", timezone]
    if description:
        args += ["--description", description]
    return _run(args) is not None


def delete_event(calendar: str, event_id: str) -> bool:
    """Delete an event. Return True on success."""
    return _run(["delete", str(event_id), "--calendar", calendar]) is not None
