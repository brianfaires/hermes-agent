"""Cross-profile cron event publication.

Cron mutations already have an in-process hook registry in :mod:`cron.hooks`.
This module adds a narrow file-backed event stream so one profile can observe
another profile's cron lifecycle without mutating that profile's scheduler
state. The first consumer is Ops' cron-calendar coordination: every opted-in
profile appends redacted cron events to a shared root-level JSONL stream that
Ops can read and aggregate.

This is intentionally publish/observe only. Requests for changes belong on the
owning profile's Kanban queue; this module never edits another profile's cron
files.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional

from hermes_constants import get_default_hermes_root, get_hermes_home
from hermes_cli.config import load_config_readonly

SCHEMA_VERSION = 1
EVENT_ROOT_ENV = "HERMES_CRON_EVENTS_DIR"
ENABLED_ENV = "HERMES_CRON_EVENTS_ENABLED"

# Fields enough for calendar/overlap analysis without copying prompts, scripts,
# origins, outputs, or other conversational/private content into the shared log.
SAFE_JOB_FIELDS = {
    "id",
    "name",
    "schedule",
    "schedule_display",
    "next_run_at",
    "last_run_at",
    "last_status",
    "enabled",
    "state",
    "paused_at",
    "paused_reason",
    "repeat",
    "skills",
    "skill",
    "model",
    "provider",
    "no_agent",
    "enabled_toolsets",
}


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def active_profile_name() -> str:
    """Return the active profile name inferred from ``HERMES_HOME``.

    The default profile uses the root Hermes home (``~/.hermes``). Named
    profiles live under ``<root>/profiles/<name>``.
    """

    # HERMES_HOME is the storage boundary that determines which cron/jobs.json
    # was mutated, so it is the source of truth for event ownership. Some
    # supervisors also export HERMES_PROFILE for the launcher profile; that can
    # be stale in tests or nested worker processes and must not override the
    # actual profile home that received the write.
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
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", profile)


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
    """Whether this profile should publish cron events to the shared stream."""

    env_value = os.environ.get(ENABLED_ENV)
    if env_value is not None:
        return _truthy(env_value)

    cfg = load_config_readonly()
    cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
    events_cfg = cron_cfg.get("events", {}) if isinstance(cron_cfg, dict) else {}
    if not isinstance(events_cfg, dict):
        return False
    return bool(events_cfg.get("enabled", False))


def event_file_for_profile(profile: Optional[str] = None) -> Path:
    """Return the JSONL event file for ``profile`` under :func:`event_root`."""

    return event_root() / f"{_safe_profile_segment(profile or active_profile_name())}.jsonl"


def _redact_job(job: Mapping[str, Any]) -> Dict[str, Any]:
    redacted = {key: job.get(key) for key in sorted(SAFE_JOB_FIELDS) if key in job}
    if "id" in job:
        redacted["id"] = str(job.get("id"))
    if "name" in job and job.get("name") is not None:
        redacted["name"] = str(job.get("name"))
    return redacted


def build_cron_event(
    event_type: str,
    *,
    job: Mapping[str, Any],
    source_profile: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a schema-versioned, redacted cron event record."""

    profile = source_profile or active_profile_name()
    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "event_type": str(event_type),
        "emitted_at": _utc_now_iso(),
        "source_profile": profile,
        "job_id": str(job.get("id", "")),
        "job": _redact_job(job),
    }
    if extra:
        # Extra payload is for non-sensitive run metadata (success/duration/error
        # summaries). It is kept separate from job so consumers can ignore it.
        record["extra"] = dict(extra)
    return record


def publish_cron_event(
    event_type: str,
    *,
    job: Mapping[str, Any],
    source_profile: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Publish a cron event to the profile's JSONL stream.

    When ``dry_run`` is true, returns the exact event record that would be
    written without touching the filesystem.
    """

    record = build_cron_event(
        event_type,
        job=job,
        source_profile=source_profile,
        extra=extra,
    )
    if dry_run:
        return record

    path = event_file_for_profile(record["source_profile"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
        handle.write("\n")
    return record


def iter_events(
    *,
    profiles: Optional[Iterable[str]] = None,
    root: Optional[Path] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield parsed event records from the shared JSONL streams."""

    base = root or event_root()
    names = list(profiles) if profiles is not None else None
    paths = [base / f"{_safe_profile_segment(name)}.jsonl" for name in names] if names else sorted(base.glob("*.jsonl"))
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)
