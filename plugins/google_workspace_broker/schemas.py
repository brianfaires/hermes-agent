from __future__ import annotations

STRING = {"type": "string", "minLength": 1}
STRING_LIST = {"type": "array", "items": STRING}
DATE_OR_DATETIME = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "date": {"type": "string"},
        "dateTime": {"type": "string"},
        "timeZone": {"type": "string"},
    },
}


def obj(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


CALENDAR_READ_SCHEMA = {
    "name": "google_calendar_read",
    "description": "Read Google Calendar calendars and events through the local broker.",
    "parameters": obj({
        "operation": {"type": "string", "enum": [
            "calendar.list_calendars", "calendar.list_events", "calendar.get_event",
        ]},
        "calendar_id": STRING,
        "event_id": STRING,
        "time_min": {"type": "string"},
        "time_max": {"type": "string"},
        "query": {"type": "string"},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 250},
    }, ["operation"]),
}

CALENDAR_MANAGE_SCHEMA = {
    "name": "google_calendar_manage",
    "description": "Create broker-owned calendars and create/update events only on broker-owned calendars.",
    "parameters": obj({
        "operation": {"type": "string", "enum": [
            "calendar.create_calendar", "calendar.create_event", "calendar.update_event",
        ]},
        "calendar_id": STRING,
        "event_id": STRING,
        "summary": STRING,
        "description": {"type": "string"},
        "location": {"type": "string"},
        "start": DATE_OR_DATETIME,
        "end": DATE_OR_DATETIME,
        "attendee_emails": STRING_LIST,
    }, ["operation"]),
}

GMAIL_READ_SCHEMA = {
    "name": "google_gmail_read",
    "description": "Search and read Gmail messages and threads through the local broker.",
    "parameters": obj({
        "operation": {"type": "string", "enum": [
            "gmail.search_messages", "gmail.get_message", "gmail.get_thread",
        ]},
        "query": {"type": "string"},
        "message_id": STRING,
        "thread_id": STRING,
        "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
    }, ["operation"]),
}

GMAIL_LABELS_SCHEMA = {
    "name": "google_gmail_labels",
    "description": "Manage user-created Gmail labels and apply/remove user labels only.",
    "parameters": obj({
        "operation": {"type": "string", "enum": [
            "gmail.list_labels", "gmail.create_label", "gmail.update_label",
            "gmail.delete_label", "gmail.modify_message_labels",
        ]},
        "label_id": STRING,
        "name": STRING,
        "message_id": STRING,
        "add_label_ids": STRING_LIST,
        "remove_label_ids": STRING_LIST,
    }, ["operation"]),
}

GMAIL_DRAFTS_SCHEMA = {
    "name": "google_gmail_drafts",
    "description": "Create Gmail drafts only; sending/replying/forwarding is not available.",
    "parameters": obj({
        "operation": {"type": "string", "enum": ["gmail.create_draft"]},
        "to": STRING_LIST,
        "cc": STRING_LIST,
        "bcc": STRING_LIST,
        "subject": STRING,
        "body_text": STRING,
    }, ["operation", "to", "subject", "body_text"]),
}
