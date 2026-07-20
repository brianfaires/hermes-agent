#!/usr/bin/env python3
"""Isolated Google Calendar API worker for the cron-calendar-sync plugin.

The worker imports the active profile's google-workspace skill, applies its
calendar write policy, and executes one JSON request. Running this boundary in
a subprocess contains google_api's CLI-style SystemExit behavior.
"""

import importlib.util
import json
import sys
from pathlib import Path

from hermes_constants import get_hermes_home


def _load_google_api():
    script = (
        get_hermes_home()
        / "skills"
        / "productivity"
        / "google-workspace"
        / "scripts"
        / "google_api.py"
    )
    if not script.exists():
        raise RuntimeError(f"google-workspace script missing at {script}")
    scripts_dir = str(script.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("hermes_cron_calendar_google_api", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load google-workspace script at {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _calendar(api, value: str, *, write_action: str | None = None):
    calendar_id = api._resolve_calendar_id(value)
    if write_action:
        api._require_calendar_action_allowed(write_action)
        api._require_calendar_write_allowed(value, calendar_id)
    return api.build_service("calendar", "v3"), calendar_id


def execute(request: dict) -> object:
    api = _load_google_api()
    operation = request["operation"]
    calendar = request["calendar"]

    if operation == "create":
        service, calendar_id = _calendar(api, calendar, write_action="create")
        return service.events().insert(calendarId=calendar_id, body=request["body"]).execute()
    if operation == "patch":
        service, calendar_id = _calendar(api, calendar, write_action="update")
        return service.events().patch(
            calendarId=calendar_id,
            eventId=request["event_id"],
            body=request["body"],
        ).execute()
    if operation == "get":
        service, calendar_id = _calendar(api, calendar)
        return service.events().get(
            calendarId=calendar_id, eventId=request["event_id"]
        ).execute()
    if operation == "list":
        service, calendar_id = _calendar(api, calendar)
        items = []
        page_token = None
        while True:
            result = service.events().list(
                calendarId=calendar_id,
                singleEvents=False,
                showDeleted=False,
                maxResults=2500,
                pageToken=page_token,
            ).execute()
            items.extend(result.get("items", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                return {"items": items}
    if operation == "instances":
        service, calendar_id = _calendar(api, calendar)
        return service.events().instances(
            calendarId=calendar_id,
            eventId=request["event_id"],
            timeMin=request["time_min"],
            timeMax=request["time_max"],
        ).execute()
    raise ValueError(f"unsupported calendar operation: {operation}")


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        print(json.dumps({"ok": True, "result": execute(request)}))
        return 0
    except BaseException as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        print(json.dumps({"ok": False, "error": str(exc), "status": status}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
