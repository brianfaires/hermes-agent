"""Typed adapter over the out-of-process Google Calendar worker.

The one invariant that matters here: *confirmed absence* and *read failure*
must never collapse into the same result. A 404/410 means the resource is gone
and the reconciler may recreate or forget it; a timeout, 403, malformed
response, or missing credential means we know nothing and the reconciler must
leave the calendar alone. Every caller in this plugin branches on that
distinction, so it is encoded in the exception type rather than in a ``None``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60



class CalendarError(RuntimeError):
    """A Calendar call failed for a reason other than confirmed absence."""


class CalendarUnavailable(CalendarError):
    """Credentials, skill, or worker are missing — mirroring cannot run."""


class CalendarNotFound(CalendarError):
    """Google confirmed (404/410) that the resource is gone."""


class CalendarConflict(CalendarError):
    """Google rejected (409) a deterministic id that already exists."""


def worker_path() -> Path:
    return Path(__file__).with_name("calendar_worker.py")


def skill_script_path(home: Optional[Path] = None) -> Path:
    root = home or get_hermes_home()
    return (
        root
        / "skills"
        / "productivity"
        / "google-workspace"
        / "scripts"
        / "google_api.py"
    )


def token_path(home: Optional[Path] = None) -> Path:
    root = home or get_hermes_home()
    return root / "google_token.json"


def missing_prerequisites() -> List[str]:
    """Return human-readable reasons this profile cannot mirror, if any."""
    home = get_hermes_home()
    missing: List[str] = []
    if not skill_script_path(home).exists():
        missing.append(f"google-workspace skill script not found at {skill_script_path(home)}")
    if not token_path(home).exists():
        missing.append(f"google-workspace OAuth token not found at {token_path(home)}")
    if not worker_path().exists():
        missing.append(f"calendar worker not found at {worker_path()}")
    return missing


def worker_python() -> str:
    from plugins.cron_calendar_mirror.mirror import raw_settings

    override = str(raw_settings().get("worker_python") or "").strip()
    return override or sys.executable


class CalendarClient:
    """Fail-soft, profile-scoped Calendar adapter.

    ``calendar`` is the configured target value; the worker resolves and
    re-guards it on every call, so a compromised in-process value cannot
    redirect a write to another calendar.
    """

    def __init__(self, calendar: str, *, approved_owner: str = "", timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.calendar = str(calendar)
        self.approved_owner = approved_owner
        self.timeout = int(timeout)

    # -- transport ---------------------------------------------------------

    def _request(self, operation: str, **payload: Any) -> Any:
        missing = missing_prerequisites()
        if missing:
            raise CalendarUnavailable("; ".join(missing))

        request = {"operation": operation, "calendar": self.calendar, "approved_owner": self.approved_owner, **payload}
        env = os.environ.copy()
        # Explicit propagation: a context-local profile home does not cross the
        # subprocess boundary, and defaulting would read another profile's token.
        env["HERMES_HOME"] = str(get_hermes_home())
        try:
            result = subprocess.run(
                [worker_python(), str(worker_path())],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
        except Exception as exc:  # timeout, OSError, ...
            raise CalendarError(f"calendar {operation} could not run ({type(exc).__name__})") from exc

        try:
            response = json.loads(result.stdout)
        except (TypeError, ValueError):
            response = None

        if not isinstance(response, dict):
            raise CalendarError(f"calendar {operation} returned a malformed response")
        if response.get("ok") is True and result.returncode == 0:
            return response.get("result")

        status = response.get("status")
        detail = str(response.get("error") or "unknown error")
        logger.warning("cron_calendar_mirror: calendar %s failed: %s", operation, detail)
        if status in (404, 410):
            raise CalendarNotFound(detail)
        if status == 409:
            raise CalendarConflict(detail)
        raise CalendarError(detail)

    # -- operations --------------------------------------------------------

    def verify(self) -> Dict[str, Any]:
        result = self._request("verify")
        if not isinstance(result, dict):
            raise CalendarError("calendar verify returned a malformed payload")
        return result

    def get_event(self, event_id: str) -> Dict[str, Any]:
        result = self._request("get", event_id=str(event_id))
        if not isinstance(result, dict):
            raise CalendarError("calendar get returned a malformed payload")
        return result

    def list_events(
        self,
        *,
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
        private_property: Sequence[str] = (),
    ) -> List[Dict[str, Any]]:
        result = self._request(
            "list",
            time_min=time_min,
            time_max=time_max,
            private_property=list(private_property),
        )
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list):
            raise CalendarError("calendar list returned a malformed payload")
        if not all(isinstance(item, dict) and item.get("id") for item in items):
            raise CalendarError("calendar list contains malformed events")
        return items

    def insert_event(self, body: Dict[str, Any]) -> Dict[str, Any]:
        result = self._request("insert", body=body)
        if not isinstance(result, dict):
            raise CalendarError("calendar insert returned a malformed payload")
        return result

    def patch_event(self, event_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        result = self._request("patch", event_id=str(event_id), body=body)
        if not isinstance(result, dict):
            raise CalendarError("calendar patch returned a malformed payload")
        return result
