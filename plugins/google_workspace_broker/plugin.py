from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .client import BrokerClient, SocketConfigError
from . import protocol
from .schemas import (
    CALENDAR_MANAGE_SCHEMA,
    CALENDAR_READ_SCHEMA,
    GMAIL_DRAFTS_SCHEMA,
    GMAIL_LABELS_SCHEMA,
    GMAIL_READ_SCHEMA,
)

_CONFIG_ENV = "GOOGLE_WORKSPACE_BROKER_CONFIG"


def _load_socket_config() -> dict[str, Any]:
    path = os.getenv(_CONFIG_ENV)
    if not path:
        return {}
    return {"__config_path__": path}


def _available() -> bool:
    try:
        config = _load_socket_config()
        path = config.get("__config_path__")
        if not isinstance(path, str):
            return False
        BrokerClient.from_config_file(Path(path))
        return True
    except Exception:
        return False


def _handler(allowed_operations: set[str]):
    def handle(args: dict[str, Any], **_: Any) -> str:
        operation = args.get("operation")
        params = {k: v for k, v in args.items() if k != "operation" and v is not None}
        if operation not in allowed_operations:
            return json.dumps({"error": "operation is not available for this tool"}, ensure_ascii=False)
        try:
            config = _load_socket_config()
            path = config.get("__config_path__")
            if not isinstance(path, str):
                raise RuntimeError("google workspace broker config is missing")
            client = BrokerClient.from_config_file(Path(path))
            return json.dumps(client.call(operation, params), ensure_ascii=False)
        except (SocketConfigError, protocol.ProtocolError, RuntimeError) as exc:
            return json.dumps({"error": protocol.sanitize_error(exc)}, ensure_ascii=False)
        except Exception:
            return json.dumps({"error": "google workspace broker call failed"}, ensure_ascii=False)
    return handle


def register(ctx) -> None:
    tools = (
        ("google_calendar_read", CALENDAR_READ_SCHEMA, _handler({
            "calendar.list_calendars", "calendar.list_events", "calendar.get_event",
        }), ""),
        ("google_calendar_manage", CALENDAR_MANAGE_SCHEMA, _handler({
            "calendar.create_calendar", "calendar.create_event", "calendar.update_event",
        }), ""),
        ("google_gmail_read", GMAIL_READ_SCHEMA, _handler({
            "gmail.search_messages", "gmail.get_message", "gmail.get_thread",
        }), ""),
        ("google_gmail_labels", GMAIL_LABELS_SCHEMA, _handler({
            "gmail.list_labels", "gmail.create_label", "gmail.update_label",
            "gmail.delete_label", "gmail.modify_message_labels",
        }), ""),
        ("google_gmail_drafts", GMAIL_DRAFTS_SCHEMA, _handler({"gmail.create_draft"}), ""),
    )
    for name, schema, handler, emoji in tools:
        ctx.register_tool(
            name=name,
            toolset=name,
            schema=schema,
            handler=handler,
            check_fn=_available,
            emoji=emoji,
        )
