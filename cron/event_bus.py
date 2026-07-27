"""Generic, atomic cross-profile cron event publication.

Cron core only publishes redacted lifecycle records.  Consumption, retry,
acknowledgement, deduplication, recovery, and retention belong to subscribers.
Each event is committed as its own file with an atomic same-directory rename so
readers never observe a partial JSON record.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from hermes_constants import get_default_hermes_root, get_hermes_home
from hermes_cli.config import load_config_readonly

SCHEMA_VERSION = 1
EVENT_ROOT_ENV = "HERMES_CRON_EVENTS_DIR"
ENABLED_ENV = "HERMES_CRON_EVENTS_ENABLED"
_SECURE_DIR_FD = (
    os.name != "nt"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
)


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def active_profile_name() -> str:
    """Return the profile that owns the active ``HERMES_HOME``."""

    home = get_hermes_home()
    try:
        resolved = home.resolve()
    except OSError:
        resolved = home
    if resolved.parent.name == "profiles" and resolved.name:
        return resolved.name
    return "default"


def _safe_profile_segment(profile: str) -> str:
    profile = profile.strip() or "default"
    segment = re.sub(r"[^A-Za-z0-9_.-]+", "_", profile)
    if segment in {".", ".."}:
        raise ValueError("profile name cannot be '.' or '..'")
    return segment


def event_root() -> Path:
    """Return the root directory for cross-profile cron events."""

    override = os.environ.get(EVENT_ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    cfg = load_config_readonly()
    cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
    events_cfg = cron_cfg.get("events", {}) if isinstance(cron_cfg, dict) else {}
    configured = events_cfg.get("directory") if isinstance(events_cfg, dict) else None
    if configured:
        return Path(str(configured)).expanduser()
    return get_default_hermes_root() / "events" / "cron"


def events_enabled() -> bool:
    """Whether this profile should publish cron lifecycle events."""

    env_value = os.environ.get(ENABLED_ENV)
    if env_value is not None:
        return _truthy(env_value)
    cfg = load_config_readonly()
    cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
    events_cfg = cron_cfg.get("events", {}) if isinstance(cron_cfg, dict) else {}
    return isinstance(events_cfg, dict) and events_cfg.get("enabled") is True


def pending_directory_for_profile(profile: Optional[str] = None) -> Path:
    """Return the publisher-owned pending directory for ``profile``."""

    return event_root() / "pending" / _safe_profile_segment(
        profile or active_profile_name()
    )


def _redacted_schedule(value: Any) -> Optional[Dict[str, Any]]:
    """Return only normalized scheduler fields, never arbitrary mapping content."""

    if not isinstance(value, Mapping):
        return None
    kind = value.get("kind")
    if kind == "interval":
        minutes = value.get("minutes")
        if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes >= 0:
            return {"kind": "interval", "minutes": minutes}
        return None
    if kind == "cron":
        expression = value.get("expr")
        if isinstance(expression, str):
            parts = expression.split()
            if len(parts) in {5, 6} and all(
                re.fullmatch(r"[\d*\-,/]+", part) for part in parts
            ):
                return {"kind": "cron", "expr": expression}
        return None
    if kind == "once":
        run_at = value.get("run_at")
        if isinstance(run_at, str):
            try:
                datetime.fromisoformat(run_at.replace("Z", "+00:00"))
            except ValueError:
                return None
            return {"kind": "once", "run_at": run_at}
    return None


def _string_list(value: Any) -> Optional[list[str]]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        return None
    return list(value)


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}$")


def _safe_name(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or _SAFE_NAME.fullmatch(text) is None:
        return None
    return text


def _safe_job_id(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value
    ) is None:
        raise ValueError("cron event job id must be a safe identifier string")
    return value


def _safe_name_list(value: Any) -> Optional[list[str]]:
    items = _string_list(value)
    if items is None:
        return None
    cleaned: list[str] = []
    for item in items:
        safe = _safe_name(item)
        if safe is None:
            return None
        cleaned.append(safe)
    return cleaned


def _redact_job(job: Mapping[str, Any]) -> Dict[str, Any]:
    """Serialize only expected scalar/list shapes from the shared-job contract."""

    redacted: Dict[str, Any] = {}
    if "id" in job:
        redacted["id"] = _safe_job_id(job.get("id"))

    schedule = _redacted_schedule(job.get("schedule"))
    if schedule is not None:
        redacted["schedule"] = schedule
        if schedule["kind"] == "interval":
            redacted["schedule_display"] = f"every {schedule['minutes']}m"
        elif schedule["kind"] == "cron":
            redacted["schedule_display"] = schedule["expr"]
        else:
            redacted["schedule_display"] = f"once at {schedule['run_at']}"

    for key in {
        "last_status",
        "state",
        "model",
        "provider",
    }:
        value = job.get(key)
        if isinstance(value, str):
            redacted[key] = value

    skill = _safe_name(job.get("skill"))
    if skill is not None:
        redacted["skill"] = skill

    for key in {"next_run_at", "last_run_at", "paused_at"}:
        value = job.get(key)
        if not isinstance(value, str):
            continue
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        redacted[key] = value

    for key in {"enabled", "no_agent"}:
        value = job.get(key)
        if isinstance(value, bool):
            redacted[key] = value

    repeat = job.get("repeat")
    if isinstance(repeat, int) and not isinstance(repeat, bool):
        redacted["repeat"] = repeat

    skills = _safe_name_list(job.get("skills"))
    if skills is not None:
        redacted["skills"] = skills

    toolsets = _safe_name_list(job.get("enabled_toolsets"))
    if toolsets is not None:
        redacted["enabled_toolsets"] = toolsets

    name = job.get("name")
    if job.get("name_is_explicit") is True and isinstance(name, str):
        redacted["name"] = name
    return redacted


def _assert_no_symlink_components(path: Path) -> None:
    """Reject existing symlinks in a filesystem path before side effects."""

    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"cron event path contains symlink component: {current}")


def _fsync_directory(path: Path) -> None:
    """Best-effort fsync for a directory entry boundary."""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


@contextmanager
def _open_directory_fd(path: Path):
    """Open every directory component without following symlinks."""

    if not _SECURE_DIR_FD:
        _assert_no_symlink_components(path)
        yield None
        return
    _assert_no_symlink_components(path)
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parts = absolute.parts[1:]
    if not parts:
        raise ValueError("cron event root cannot be the filesystem root")
    descriptor = os.open(Path(absolute.anchor) / parts[0], flags)
    try:
        for part in parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_or_create_directory_fd(path: Path):
    """Create missing components and retain the final pinned directory FD."""

    if not _SECURE_DIR_FD:
        _ensure_directory(path)
        with _open_directory_fd(path) as descriptor:
            yield descriptor
        return
    _assert_no_symlink_components(path)
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parts = absolute.parts[1:]
    if not parts:
        raise ValueError("cron event root cannot be the filesystem root")
    descriptor = os.open(Path(absolute.anchor) / parts[0], flags)
    try:
        for part in parts[1:]:
            created = False
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    pass
                os.fsync(descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            if created:
                try:
                    os.fchmod(child, 0o700)
                except OSError:
                    pass
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _ensure_directory(path: Path) -> None:
    """Create a private directory hierarchy and persist each new parent entry."""

    if _SECURE_DIR_FD:
        with _open_or_create_directory_fd(path):
            pass
        return

    _assert_no_symlink_components(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        _fsync_directory(directory.parent)
    _assert_no_symlink_components(path)


def build_cron_event(
    event_type: str,
    *,
    job: Mapping[str, Any],
    source_profile: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a schema-versioned, redacted cron event record."""

    profile = _safe_profile_segment(source_profile or active_profile_name())
    job_id = _safe_job_id(job.get("id"))
    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "event_type": str(event_type),
        "emitted_at": _utc_now_iso(),
        "source_profile": profile,
        "job_id": job_id,
        "job": _redact_job(job),
    }
    if extra:
        safe_extra: Dict[str, Any] = {}
        for key in {"success", "error_present"}:
            value = extra.get(key)
            if isinstance(value, bool):
                safe_extra[key] = value
        duration = extra.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            safe_extra["duration_seconds"] = duration
        if safe_extra:
            record["extra"] = safe_extra
    return record


def _commit_record(record: Mapping[str, Any]) -> Path:
    directory = pending_directory_for_profile(str(record["source_profile"]))
    prefix = f"{time.time_ns():020d}-{record['event_id']}"
    destination = directory / f"{prefix}.json"
    temporary = directory / f".{prefix}.tmp"
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"

    with _open_or_create_directory_fd(directory) as directory_fd:
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if directory_fd is None:
                descriptor = os.open(temporary, flags, 0o600)
            else:
                descriptor = os.open(
                    temporary.name, flags, 0o600, dir_fd=directory_fd
                )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if directory_fd is None:
                os.replace(temporary, destination)
                _fsync_directory(directory)
            else:
                os.replace(
                    temporary.name,
                    destination.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
                os.fsync(directory_fd)
        except BaseException:
            try:
                if directory_fd is None:
                    temporary.unlink(missing_ok=True)
                else:
                    os.unlink(temporary.name, dir_fd=directory_fd)
            except OSError:
                pass
            raise
    return destination


def publish_cron_event(
    event_type: str,
    *,
    job: Mapping[str, Any],
    source_profile: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Atomically publish one redacted cron event file.

    ``dry_run`` returns the exact record without touching the filesystem.
    """

    record = build_cron_event(
        event_type,
        job=job,
        source_profile=source_profile,
        extra=extra,
    )
    if not dry_run:
        _commit_record(record)
    return record
