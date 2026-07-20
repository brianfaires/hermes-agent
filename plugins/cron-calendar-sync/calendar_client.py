"""Fail-soft adapter to the active profile's google-workspace Calendar API."""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


class CalendarOperationError(RuntimeError):
    """A Calendar read failed for a reason other than confirmed absence."""


class CalendarNotFoundError(CalendarOperationError):
    """Google confirmed that the requested Calendar resource is gone."""


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


def _worker_path() -> Path:
    return Path(__file__).with_name("calendar_worker.py")


def available() -> bool:
    """True when this profile has the skill, OAuth token, and worker."""
    return _script_path().exists() and _token_path().exists() and _worker_path().exists()


def _run_request(operation: str, calendar: str, **payload) -> Optional[object]:
    request = {"operation": operation, "calendar": calendar, **payload}
    env = os.environ.copy()
    env["HERMES_HOME"] = str(get_hermes_home())
    try:
        result = subprocess.run(
            [sys.executable, str(_worker_path())],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except Exception as exc:
        logger.error("cron_calendar_sync: calendar %s failed to run: %s", operation, exc)
        raise CalendarOperationError(str(exc)) from exc
    try:
        response = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        response = None
    if result.returncode != 0 or not isinstance(response, dict) or not response.get("ok"):
        detail = response.get("error") if isinstance(response, dict) else None
        status = response.get("status") if isinstance(response, dict) else None
        detail = detail or (result.stderr or result.stdout or "unknown error").strip()
        logger.warning("cron_calendar_sync: calendar %s failed: %s", operation, detail)
        if status in {404, 410}:
            raise CalendarNotFoundError(detail)
        raise CalendarOperationError(detail)
    return response.get("result")


def create_event_body(calendar: str, body: dict) -> Optional[str]:
    try:
        result = _run_request("create", calendar, body=body)
    except CalendarOperationError:
        return None
    return result.get("id") if isinstance(result, dict) else None


def patch_event_body(calendar: str, event_id: str, body: dict) -> bool:
    try:
        return _run_request("patch", calendar, event_id=str(event_id), body=body) is not None
    except CalendarOperationError:
        return False


def get_event(calendar: str, event_id: str) -> Optional[dict]:
    try:
        result = _run_request("get", calendar, event_id=str(event_id))
    except CalendarNotFoundError:
        return None
    return result if isinstance(result, dict) else None


def list_events(calendar: str) -> list[dict]:
    result = _run_request("list", calendar)
    return result.get("items", []) if isinstance(result, dict) else []


def list_instances(
    calendar: str, event_id: str, time_min: str, time_max: str
) -> list[dict]:
    result = _run_request(
        "instances",
        calendar,
        event_id=str(event_id),
        time_min=time_min,
        time_max=time_max,
    )
    return result.get("items", []) if isinstance(result, dict) else []
