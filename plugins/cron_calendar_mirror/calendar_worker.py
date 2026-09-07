#!/usr/bin/env python3
"""Isolated Google Calendar API worker for the cron_calendar_mirror plugin.

One JSON request on stdin, one JSON response on stdout. Running this boundary
in a subprocess contains ``google_api.py``'s CLI-style ``SystemExit`` behaviour
and keeps the (optional) Google client libraries out of the agent process.

The parent MUST pass ``HERMES_HOME`` explicitly: a context-local profile home
does not cross a subprocess boundary, and silently falling back to the default
profile would read another profile's token (see the parity reference).

Write policy is enforced twice on purpose. The profile's google-workspace skill
may or may not carry an additional fail-closed policy layer, so this worker also applies its own unconditional guard:
never ``primary``, never a bare/unqualified calendar value, and never any
guest/notification field that could widen who sees a private run result.
"""

import importlib.util
import json
import sys
from pathlib import Path

from hermes_constants import get_hermes_home

# Event fields this worker refuses to forward under any circumstance. Each one
# either widens the audience of an event or emits an outbound notification.
FORBIDDEN_EVENT_FIELDS = frozenset({
    "attendees",
    "attendeesOmitted",
    "attachments",
    "recurrence",
    "conferenceData",
    "gadget",
    "organizer",
    "originalStartTime",
    "recurringEventId",
    "source",
})


class WorkerPolicyError(RuntimeError):
    """The request violated this worker's unconditional write policy."""


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
    spec = importlib.util.spec_from_file_location(
        "hermes_cron_calendar_mirror_google_api", script
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load google-workspace script at {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _guard_target(calendar_value: str, resolved_id: str) -> None:
    """Unconditional target guard, independent of the skill's policy layer."""
    for value in (calendar_value, resolved_id):
        text = str(value or "").strip()
        if not text:
            raise WorkerPolicyError("calendar id is empty")
        if text.lower() == "primary":
            raise WorkerPolicyError("refusing to touch the primary calendar")
    if not str(resolved_id).endswith("@group.calendar.google.com"):
        raise WorkerPolicyError("refusing unverified secondary calendar routing")


def _guard_body(body: dict) -> dict:
    """Reject audience-widening fields; force private, silent, guest-free."""
    if not isinstance(body, dict):
        raise WorkerPolicyError("event body must be an object")
    offending = sorted(FORBIDDEN_EVENT_FIELDS.intersection(body))
    if offending:
        raise WorkerPolicyError(f"event body carries forbidden fields: {offending}")
    guarded = dict(body)
    guarded["visibility"] = "private"
    guarded["guestsCanInviteOthers"] = False
    guarded["guestsCanModify"] = False
    guarded["guestsCanSeeOtherGuests"] = False
    guarded["reminders"] = {"useDefault": False, "overrides": []}
    return guarded


def _resolve(api, value: str) -> str:
    resolver = getattr(api, "_resolve_calendar_id", None)
    if callable(resolver):
        return str(resolver(value))
    return str(value)


def _apply_skill_policy(api, calendar_value: str, resolved_id: str, action: str) -> None:
    """Apply the skill's own fail-closed policy layer when it exists."""
    action_gate = getattr(api, "_require_calendar_action_allowed", None)
    if callable(action_gate):
        action_gate(action)
    write_gate = getattr(api, "_require_calendar_write_allowed", None)
    if callable(write_gate):
        write_gate(calendar_value, resolved_id)


def _calendar(api, value: str, *, owner: str, write_action=None):
    calendar_id = _resolve(api, value)
    _guard_target(value, calendar_id)
    if calendar_id != value or not owner or "@" not in owner:
        raise WorkerPolicyError("explicit calendar id and approved_owner required")
    if write_action:
        _apply_skill_policy(api, value, calendar_id, write_action)
    # The installed skill can default to broad scopes when token metadata is
    # absent. Refuse that fallback, and reuse only explicitly granted scopes.
    token = getattr(api, "TOKEN_PATH", None)
    if token is None:
        raise WorkerPolicyError("google-workspace credential path unavailable")
    if token is not None:
        scopes = json.loads(Path(token).read_text()).get("scopes")
        if not isinstance(scopes, list) or not scopes or not all(isinstance(x, str) for x in scopes):
            raise WorkerPolicyError("granted scope metadata unavailable")
        api.SCOPES = scopes
        api._stored_token_scopes = lambda: scopes
    service = api.build_service("calendar", "v3")
    entry = service.calendarList().get(calendarId=calendar_id).execute()
    if (not isinstance(entry, dict) or entry.get("id") != calendar_id
            or entry.get("primary", False) is not False or entry.get("accessRole") != "owner"):
        raise WorkerPolicyError("target ownership/secondary routing not verified")
    rules = []
    page = None
    seen = set()
    while True:
        acl = service.acl().list(calendarId=calendar_id, pageToken=page).execute()
        if not isinstance(acl, dict) or not isinstance(acl.get("items"), list):
            raise WorkerPolicyError("malformed ACL evidence")
        rules.extend(acl["items"])
        page = acl.get("nextPageToken")
        if not page:
            break
        if not isinstance(page, str) or page in seen:
            raise WorkerPolicyError("malformed ACL pagination")
        seen.add(page)
    if len(rules) != 1 or not isinstance(rules[0], dict):
        raise WorkerPolicyError("ACL differs from approved owner-only policy")
    rule = rules[0]
    if rule.get("role") != "owner" or rule.get("scope") != {"type": "user", "value": owner}:
        raise WorkerPolicyError("ACL differs from approved owner-only policy")
    return service, calendar_id, dict(entry, primary=False, aclVerified=True, approvedOwner=owner)


def _guard_existing(event, body):
    if (not isinstance(event, dict) or event.get("status") == "cancelled"
            or event.get("visibility") != "private" or event.get("attendees")
            or event.get("attendeesOmitted") or event.get("recurrence")
            or event.get("recurringEventId") or not event.get("etag")):
        raise WorkerPolicyError("existing event privacy/integrity not verified")
    expected = body.get("extendedProperties", {}).get("private", {})
    actual = event.get("extendedProperties", {}).get("private", {})
    keys = ("hermesMirrorProfile", "hermesMirrorKind", "hermesMirrorJobId",
            "hermesMirrorExecutionId", "hermesMirrorOccurrence")
    if not expected or any(actual.get(k) != expected.get(k) for k in keys):
        raise WorkerPolicyError("existing event identity mismatch")


def execute(request: dict) -> object:
    api = _load_google_api()
    operation = request["operation"]
    calendar = request["calendar"]

    owner = str(request.get("approved_owner") or "")
    if operation == "verify":
        return _calendar(api, calendar, owner=owner)[2]

    if operation == "get":
        service, calendar_id, _ = _calendar(api, calendar, owner=owner)
        return service.events().get(
            calendarId=calendar_id, eventId=request["event_id"]
        ).execute()

    if operation == "list":
        service, calendar_id, _ = _calendar(api, calendar, owner=owner)
        items = []
        page_token = None
        seen = set()
        while True:
            result = service.events().list(
                calendarId=calendar_id,
                singleEvents=False,
                showDeleted=False,
                maxResults=2500,
                timeMin=request.get("time_min"),
                timeMax=request.get("time_max"),
                privateExtendedProperty=list(request.get("private_property") or []),
                pageToken=page_token,
            ).execute()
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise WorkerPolicyError("malformed event listing")
            items.extend(result["items"])
            page_token = result.get("nextPageToken")
            if not page_token:
                return {"items": items}
            if not isinstance(page_token, str) or page_token in seen:
                raise WorkerPolicyError("malformed event pagination")
            seen.add(page_token)

    if operation == "insert":
        service, calendar_id, _ = _calendar(api, calendar, owner=owner, write_action="create")
        return service.events().insert(
            calendarId=calendar_id,
            body=_guard_body(request["body"]),
            sendUpdates="none",
            supportsAttachments=False,
        ).execute()

    if operation == "patch":
        service, calendar_id, _ = _calendar(api, calendar, owner=owner, write_action="update")
        body = _guard_body(request["body"])
        current = service.events().get(calendarId=calendar_id, eventId=request["event_id"]).execute()
        _guard_existing(current, body)
        call = service.events().patch(
            calendarId=calendar_id, eventId=request["event_id"], body=body,
            sendUpdates="none", supportsAttachments=False,
        )
        # Guest/visibility changes between the read and patch must fail (412).
        call.headers["If-Match"] = current["etag"]
        return call.execute()

    raise ValueError(f"unsupported calendar operation: {operation}")


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        print(json.dumps({"ok": True, "result": execute(request)}))
        return 0
    except BaseException as exc:  # noqa: BLE001 - boundary must never traceback
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status is None:
            status = getattr(exc, "status_code", None)
        if isinstance(exc, WorkerPolicyError):
            status = 403
        print(json.dumps({
            "ok": False,
            "error": str(exc) if isinstance(exc, WorkerPolicyError) else type(exc).__name__,
            "status": int(status) if isinstance(status, int) else None,
        }))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
