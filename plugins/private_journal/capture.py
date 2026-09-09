"""Secure holding-store capture for private journal `/log` entries."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
USAGE = "Usage: /log <text>"
_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{12}$")


class PrivateJournalStoreError(RuntimeError):
    """Raised when the private journal holding store cannot publish safely."""


def holding_dir(home=None) -> Path:
    from .storage import directory
    home = Path(home if home is not None else get_hermes_home())
    path = home / "journal" / "holding"
    with directory(home / "journal", create=True):
        pass
    with directory(path, create=True):
        pass
    return path


def _safe_source(source: Mapping[str, Any] | None) -> dict[str, str]:
    if not source:
        return {}
    allowed = ("platform", "chat_type", "profile")
    out: dict[str, str] = {}
    for key in allowed:
        val = source.get(key)
        if val is not None:
            out[key] = str(val)
    return out


def _new_id(captured_at: datetime) -> str:
    return f"{captured_at.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(6)}"


def validate_entry_id(entry_id: str) -> str:
    if not _ID_RE.fullmatch(entry_id or ""):
        raise ValueError("invalid private journal entry id")
    return entry_id


def capture_record(raw_text: str, source: Mapping[str, Any] | None = None, *, home=None) -> dict[str, Any]:
    captured_at = datetime.now().astimezone()
    entry_id = _new_id(captured_at)
    return {
        "schema_version": SCHEMA_VERSION,
        "id": entry_id,
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "timezone": str(captured_at.tzinfo),
        "text": raw_text,
        "source": _safe_source(source),
        "memory_retention": _memory_retention_policy(home=home),
    }


def publish_record(record: Mapping[str, Any], *, home=None) -> Path:
    from .storage import publish
    entry_id = validate_entry_id(str(record.get("id") or ""))
    path = holding_dir(home) / f"{entry_id}.json"
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    publish(path, payload, verify=False)
    return path


def _memory_retention_policy(*, home=None) -> dict[str, Any]:
    """Capture the immutable opt-in state without touching any provider."""
    try:
        from .runtime import memory_retention_policy

        return memory_retention_policy(home=home if home is not None else get_hermes_home())
    except Exception:
        return {"schema_version": 1, "enabled": False}


def capture_log(raw_text: str, source: Mapping[str, Any] | None = None, *, home=None) -> str:
    if raw_text is None:
        raw_text = ""
    if not str(raw_text).strip():
        return USAGE
    record = capture_record(str(raw_text), source=source, home=home)
    try:
        publish_record(record, home=home)
    except Exception as exc:
        raise PrivateJournalStoreError("private capture failed") from exc
    return f"Logged {record['id']} at {record['captured_at']}"
