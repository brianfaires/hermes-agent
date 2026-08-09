from __future__ import annotations

import json
import re
from typing import Any, Callable

from .calendar_state import CalendarOwnershipState
from .errors import CalendarStateError, PolicyError
from . import protocol


SYSTEM_LABEL_IDS = {
    "INBOX", "SPAM", "TRASH", "UNREAD", "STARRED", "IMPORTANT", "SENT",
    "DRAFT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}
SAFE_MESSAGE_SYSTEM_LABEL_IDS = {"INBOX", "UNREAD", "STARRED", "IMPORTANT"}
USER_LABEL_RE = re.compile(r"^Label_[A-Za-z0-9_-]+$")
FORBIDDEN_PARAM_NAMES = {"action", "endpoint", "url", "path", "body", "method", "token", "access_token"}


def _require_keys(params: dict[str, Any], allowed: set[str], required: set[str]) -> None:
    extra = set(params) - allowed
    if extra:
        raise PolicyError(f"unexpected parameter: {sorted(extra)[0]}")
    missing = required - set(params)
    if missing:
        raise PolicyError(f"missing parameter: {sorted(missing)[0]}")
    if FORBIDDEN_PARAM_NAMES & set(params):
        raise PolicyError("forbidden generic or credential parameter")


def _nonempty_string(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise PolicyError(f"{key} must be a non-empty string")
    return value


def _optional_string(params: dict[str, Any], key: str) -> None:
    value = params.get(key)
    if value is not None and not isinstance(value, str):
        raise PolicyError(f"{key} must be a string")


def _positive_int(params: dict[str, Any], key: str, *, maximum: int) -> None:
    value = params.get(key)
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > maximum:
        raise PolicyError(f"{key} must be an integer between 1 and {maximum}")


def _string_list(params: dict[str, Any], key: str, *, required: bool = False) -> list[str]:
    value = params.get(key)
    if value is None:
        if required:
            raise PolicyError(f"{key} is required")
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise PolicyError(f"{key} must be a list of non-empty strings")
    return value


def _date_or_datetime(params: dict[str, Any], key: str) -> None:
    value = params.get(key)
    if not isinstance(value, dict):
        raise PolicyError(f"{key} must be a date/datetime object")
    allowed = {"date", "dateTime", "timeZone"}
    extra = set(value) - allowed
    if extra:
        raise PolicyError(f"unexpected {key} field: {sorted(extra)[0]}")
    if not (value.get("date") or value.get("dateTime")):
        raise PolicyError(f"{key} must include date or dateTime")
    for field, field_value in value.items():
        if not isinstance(field_value, str) or not field_value:
            raise PolicyError(f"{key}.{field} must be a non-empty string")


def _validate_user_label_id(label_id: str) -> None:
    if label_id.upper() in SYSTEM_LABEL_IDS:
        raise PolicyError("system labels cannot be modified or impersonated")
    if not USER_LABEL_RE.fullmatch(label_id):
        raise PolicyError("invalid user label id")


def _validate_message_label_id(label_id: str) -> None:
    if label_id in SAFE_MESSAGE_SYSTEM_LABEL_IDS:
        return
    if label_id.upper() in SYSTEM_LABEL_IDS or label_id.startswith("CATEGORY_"):
        raise PolicyError("unsafe system label id")
    if not USER_LABEL_RE.fullmatch(label_id):
        raise PolicyError("invalid message label id")


def _validate_user_label_name(name: str) -> None:
    if name.upper() in SYSTEM_LABEL_IDS:
        raise PolicyError("system labels cannot be impersonated")


class Broker:
    def __init__(self, backend: Any, calendar_state: CalendarOwnershipState):
        self.backend = backend
        self.calendar_state = calendar_state
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "calendar.list_calendars": self._calendar_list_calendars,
            "calendar.list_events": self._calendar_list_events,
            "calendar.get_event": self._calendar_get_event,
            "calendar.create_calendar": self._calendar_create_calendar,
            "calendar.create_event": self._calendar_create_event,
            "calendar.update_event": self._calendar_update_event,
            "gmail.search_messages": self._gmail_search_messages,
            "gmail.get_message": self._gmail_get_message,
            "gmail.get_thread": self._gmail_get_thread,
            "gmail.list_labels": self._gmail_list_labels,
            "gmail.create_label": self._gmail_create_label,
            "gmail.update_label": self._gmail_update_label,
            "gmail.delete_label": self._gmail_delete_label,
            "gmail.modify_message_labels": self._gmail_modify_message_labels,
            "gmail.create_draft": self._gmail_create_draft,
        }

    def dispatch(self, message: dict[str, Any]) -> Any:
        if set(message) != {"operation", "params"} or not isinstance(message.get("params"), dict):
            raise PolicyError("invalid broker message")
        operation = message["operation"]
        if not isinstance(operation, str):
            raise PolicyError("invalid broker operation")
        if operation not in self._handlers:
            raise PolicyError("operation is not allowed")
        return self._handlers[operation](message["params"])

    def handle_wire(self, data: bytes) -> bytes:
        try:
            request = protocol.decode_message(data)
            result = self.dispatch(request)
            return protocol.encode_response({"ok": True, "result": result})
        except (PolicyError, CalendarStateError, protocol.ProtocolError) as exc:
            return protocol.encode_response({"ok": False, "error": protocol.sanitize_error(exc)})
        except Exception:
            return protocol.encode_response({"ok": False, "error": "broker operation failed"})

    def _calendar_list_calendars(self, params):
        _require_keys(params, {"max_results"}, set())
        _positive_int(params, "max_results", maximum=250)
        return self.backend.list_calendars(params)

    def _calendar_list_events(self, params):
        _require_keys(params, {"calendar_id", "time_min", "time_max", "query", "max_results"}, {"calendar_id"})
        _nonempty_string(params, "calendar_id")
        _optional_string(params, "time_min")
        _optional_string(params, "time_max")
        _optional_string(params, "query")
        _positive_int(params, "max_results", maximum=250)
        return self.backend.list_events(params)

    def _calendar_get_event(self, params):
        _require_keys(params, {"calendar_id", "event_id"}, {"calendar_id", "event_id"})
        _nonempty_string(params, "calendar_id")
        _nonempty_string(params, "event_id")
        return self.backend.get_event(params)

    def _calendar_create_calendar(self, params):
        _require_keys(params, {"summary", "description", "location"}, {"summary"})
        _nonempty_string(params, "summary")
        _optional_string(params, "description")
        _optional_string(params, "location")
        self.calendar_state.preflight_writable()
        result = self.backend.create_calendar(params)
        calendar_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(calendar_id, str) or not calendar_id:
            raise PolicyError("backend did not return a calendar id")
        self.calendar_state.add(calendar_id)
        return result

    def _require_owned_calendar(self, params):
        calendar_id = _nonempty_string(params, "calendar_id")
        if not self.calendar_state.contains(calendar_id):
            raise PolicyError("calendar is not broker-owned")

    def _calendar_create_event(self, params):
        _require_keys(params, {
            "calendar_id", "summary", "description", "location", "start", "end", "attendee_emails",
        }, {"calendar_id", "summary", "start", "end"})
        self._require_owned_calendar(params)
        _nonempty_string(params, "summary")
        _optional_string(params, "description")
        _optional_string(params, "location")
        _date_or_datetime(params, "start")
        _date_or_datetime(params, "end")
        _string_list(params, "attendee_emails")
        return self.backend.create_event(params)

    def _calendar_update_event(self, params):
        _require_keys(params, {
            "calendar_id", "event_id", "summary", "description", "location", "start", "end", "attendee_emails",
        }, {"calendar_id", "event_id"})
        self._require_owned_calendar(params)
        _nonempty_string(params, "event_id")
        if "summary" in params:
            _nonempty_string(params, "summary")
        _optional_string(params, "description")
        _optional_string(params, "location")
        if "start" in params:
            _date_or_datetime(params, "start")
        if "end" in params:
            _date_or_datetime(params, "end")
        _string_list(params, "attendee_emails")
        return self.backend.update_event(params)

    def _gmail_search_messages(self, params):
        _require_keys(params, {"query", "max_results"}, set())
        _optional_string(params, "query")
        _positive_int(params, "max_results", maximum=100)
        return self.backend.search_messages(params)

    def _gmail_get_message(self, params):
        _require_keys(params, {"message_id"}, {"message_id"})
        _nonempty_string(params, "message_id")
        return self.backend.get_message(params)

    def _gmail_get_thread(self, params):
        _require_keys(params, {"thread_id"}, {"thread_id"})
        _nonempty_string(params, "thread_id")
        return self.backend.get_thread(params)

    def _gmail_list_labels(self, params):
        _require_keys(params, set(), set())
        return self.backend.list_labels(params)

    def _gmail_create_label(self, params):
        _require_keys(params, {"name"}, {"name"})
        _validate_user_label_name(_nonempty_string(params, "name"))
        return self.backend.create_label(params)

    def _gmail_update_label(self, params):
        _require_keys(params, {"label_id", "name"}, {"label_id", "name"})
        _validate_user_label_id(_nonempty_string(params, "label_id"))
        _validate_user_label_name(_nonempty_string(params, "name"))
        return self.backend.update_label(params)

    def _gmail_delete_label(self, params):
        _require_keys(params, {"label_id"}, {"label_id"})
        _validate_user_label_id(_nonempty_string(params, "label_id"))
        return self.backend.delete_label(params)

    def _gmail_modify_message_labels(self, params):
        _require_keys(params, {"message_id", "add_label_ids", "remove_label_ids"}, {"message_id"})
        _nonempty_string(params, "message_id")
        for field in ("add_label_ids", "remove_label_ids"):
            labels = _string_list(params, field)
            for label_id in labels:
                _validate_message_label_id(label_id)
        return self.backend.modify_message_labels(params)

    def _gmail_create_draft(self, params):
        _require_keys(params, {"to", "cc", "bcc", "subject", "body_text"}, {"to", "subject", "body_text"})
        _string_list(params, "to", required=True)
        _string_list(params, "cc")
        _string_list(params, "bcc")
        _nonempty_string(params, "subject")
        _nonempty_string(params, "body_text")
        return self.backend.create_draft(params)
