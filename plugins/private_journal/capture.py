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


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_private_dir(path: Path, *, create_parent: bool = False) -> None:
    if path.exists() or path.is_symlink():
        try:
            st = path.lstat()
        except OSError as exc:
            raise PrivateJournalStoreError(f"cannot inspect {path.name}") from exc
        if os.path.islink(path):
            raise PrivateJournalStoreError(f"refusing symlink directory: {path.name}")
        if not os.path.isdir(path):
            raise PrivateJournalStoreError(f"not a directory: {path.name}")
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        return
    parent = path.parent
    if create_parent and parent != path and not parent.exists():
        _ensure_private_dir(parent, create_parent=True)
    if parent.is_symlink() or not parent.is_dir():
        raise PrivateJournalStoreError(f"unsafe parent directory: {parent.name}")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        _ensure_private_dir(path)
        return
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    _fsync_dir(parent)


def holding_dir() -> Path:
    home = Path(get_hermes_home()).expanduser()
    if home.is_symlink():
        raise PrivateJournalStoreError("unsafe HERMES_HOME")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    journal = home / "journal"
    holding = journal / "holding"
    _ensure_private_dir(journal)
    _ensure_private_dir(holding)
    return holding


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
    if not _ID_RE.match(entry_id or ""):
        raise ValueError("invalid private journal entry id")
    return entry_id


def capture_record(raw_text: str, source: Mapping[str, Any] | None = None) -> dict[str, Any]:
    captured_at = datetime.now().astimezone()
    entry_id = _new_id(captured_at)
    return {
        "schema_version": SCHEMA_VERSION,
        "id": entry_id,
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "text": raw_text,
        "source": _safe_source(source),
    }


def publish_record(record: Mapping[str, Any]) -> Path:
    entry_id = validate_entry_id(str(record.get("id") or ""))
    directory = holding_dir()
    final_path = directory / f"{entry_id}.json"
    tmp_path = directory / f".{entry_id}.{secrets.token_hex(4)}.tmp"
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(tmp_path, flags, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(payload)
            fh.write(b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp_path, final_path)
        try:
            os.chmod(final_path, 0o600)
        except OSError:
            pass
        _fsync_dir(directory)
        return final_path
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def capture_log(raw_text: str, source: Mapping[str, Any] | None = None) -> str:
    if raw_text is None:
        raw_text = ""
    if not str(raw_text).strip():
        return USAGE
    record = capture_record(str(raw_text), source=source)
    publish_record(record)
    logger.info(
        "captured private journal record id=%s status=holding",
        record["id"],
    )
    return f"Logged {record['id']} at {record['captured_at']}"
