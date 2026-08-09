"""Thin lifecycle bridge to the Ops-owned cron Calendar reconciler.

Calendar policy, credentials, state, locking, and reconciliation live in the
Ops runner.  This bundled plugin only forwards immutable lifecycle snapshots;
that keeps every profile's hook on the same canonical Calendar projection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from cron import hooks

logger = logging.getLogger(__name__)

OPS_HOME = Path("/home/brian/.hermes/profiles/ops").resolve()
# Authoritative Calendar ownership lives in the Ops profile. This bridge only
# relays lifecycle snapshots to that reconciler; it never mutates Calendar.
OPS_RECONCILER = OPS_HOME / "scripts" / "cron_calendar_recurring_sync.py"
# The runner ships with this plugin so a lifecycle hook never depends on an
# uncommitted worktree or a mutable profile-local implementation.
OPS_RUNNER = Path(__file__).resolve().with_name("ops_runner.py")
MANAGED_HOMES = {
    Path("/home/brian/.hermes").resolve(),
    OPS_HOME,
}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}$")
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_PATH_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MANAGED_PROFILES = {
    Path("/home/brian/.hermes").resolve(): "default",
    OPS_HOME: "ops",
}


def _enabled() -> bool:
    # The legacy calendar_sync flag guarded an in-process Calendar writer that
    # caused duplicates. The relay is the replacement path and must keep
    # forwarding lifecycle events to Ops even when that old config is disabled.
    return True


def _safe_output_file(value: object, job: dict[str, Any]) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        path = Path(value).resolve(strict=True)
    except OSError:
        return None
    raw_job_id = job.get("id")
    if not isinstance(raw_job_id, str):
        return None
    job_id = raw_job_id.strip()
    if (
        not job_id
        or job_id != raw_job_id
        or job_id in {".", ".."}
        or "/" in job_id
        or "\\" in job_id
        or path.suffix != ".md"
    ):
        return None
    return str(path) if any(
        path.is_relative_to(home / "cron" / "output" / job_id)
        for home in MANAGED_HOMES
    ) else None


def _safe_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or _SAFE_NAME.fullmatch(text) is None:
        return None
    return text


def _safe_job_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text != value or _SAFE_JOB_ID.fullmatch(text) is None:
        return None
    return text


def _safe_name_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    cleaned: list[str] = []
    for item in value:
        safe = _safe_name(item)
        if safe is None:
            return None
        cleaned.append(safe)
    return cleaned


def _safe_delivery(value: Any) -> tuple[str | None, list[dict[str, str]] | None]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)) and all(
        isinstance(item, str) for item in value
    ):
        values = list(value)
    else:
        return None, None
    channels: list[str] = []
    revision_targets: list[dict[str, str]] = []
    for value_item in values:
        for raw_target in value_item.split(","):
            target = raw_target.strip()
            if not target:
                return None, None
            channel = target.split(":", 1)[0].strip()
            safe_channel = _safe_name(channel)
            if safe_channel is None:
                return None, None
            if safe_channel not in channels:
                channels.append(safe_channel)
            target_digest = hashlib.sha256(target.encode("utf-8")).hexdigest()
            revision_targets.append(
                {"channel": safe_channel, "target_sha256": target_digest}
            )
    if not channels:
        return None, None
    return ",".join(channels), revision_targets


def _safe_script(value: Any, source_home: Path) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text != value or "\\" in text:
        return None
    raw = Path(text).expanduser()
    if raw.is_absolute():
        try:
            resolved = raw.resolve(strict=False)
        except OSError:
            return None
        scripts_dir = source_home / "scripts"
        if not resolved.is_relative_to(scripts_dir):
            return None
        relative = resolved.relative_to(source_home)
        parts = relative.parts
    else:
        parts = raw.parts
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or any(_SAFE_PATH_PART.fullmatch(part) is None for part in parts)
    ):
        return None
    return Path(*parts).as_posix()


def _iso_datetime(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _safe_schedule(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(value, dict):
        return None, None
    kind = value.get("kind")
    if kind == "cron":
        expr = value.get("expr")
        if isinstance(expr, str):
            parts = expr.split()
            if len(parts) in {5, 6} and all(
                re.fullmatch(r"[\d*\-,/]+", part) for part in parts
            ):
                return {"kind": "cron", "expr": expr}, expr
        return None, None
    if kind == "interval":
        for key, suffix in (("seconds", "s"), ("minutes", "m"), ("hours", "h")):
            raw = value.get(key)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                return {"kind": "interval", key: raw}, f"every {raw}{suffix}"
        return None, None
    if kind == "once":
        run_at = _iso_datetime(value.get("run_at"))
        if run_at:
            return {"kind": "once", "run_at": run_at}, f"once at {run_at}"
    return None, None


def _safe_repeat(value: Any) -> dict[str, int | None] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, int | None] = {}
    for key in ("times", "completed"):
        raw = value.get(key)
        if raw is None and key == "times":
            result[key] = None
        elif isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            result[key] = raw
    return result or None


def _record_revision(contract: dict[str, Any]) -> str:
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redacted_job_snapshot(
    job: dict[str, Any], profile: str, source_home: Path
) -> dict[str, Any] | None:
    redacted: dict[str, Any] = {"__profile": profile}
    revision_contract: dict[str, Any] = {}
    job_id = _safe_job_id(job.get("id"))
    if job_id is None:
        return None
    redacted["id"] = job_id

    if job.get("name_is_explicit") is True and isinstance(job.get("name"), str):
        redacted["name"] = job["name"]
        redacted["name_is_explicit"] = True

    schedule, display = _safe_schedule(job.get("schedule"))
    if schedule is not None:
        redacted["schedule"] = schedule
        redacted["schedule_display"] = display

    for key in ("enabled", "no_agent"):
        if isinstance(job.get(key), bool):
            redacted[key] = job[key]

    for key in ("state", "skill", "model", "provider", "last_status"):
        safe = _safe_name(job.get(key))
        if safe is not None:
            redacted[key] = safe

    deliver, deliver_revision = _safe_delivery(job.get("deliver"))
    if deliver is not None and deliver_revision is not None:
        redacted["deliver"] = deliver
        revision_contract["delivery"] = deliver_revision

    script = _safe_script(job.get("script"), source_home)
    if script is not None:
        redacted["script"] = script
        revision_contract["script"] = script

    for key in ("created_at", "next_run_at", "last_run_at", "paused_at"):
        value = _iso_datetime(job.get(key))
        if value is not None:
            redacted[key] = value

    for key in ("skills", "enabled_toolsets", "context_from"):
        values = _safe_name_list(job.get(key))
        if values is not None:
            redacted[key] = values

    repeat = _safe_repeat(job.get("repeat"))
    if repeat is not None:
        redacted["repeat"] = repeat

    for key in ("skills", "skill", "model", "provider", "enabled_toolsets", "no_agent", "repeat"):
        if key in redacted:
            revision_contract[key] = redacted[key]

    redacted["record_revision"] = _record_revision(revision_contract)
    return redacted


def _snapshot(job: dict[str, Any]) -> dict[str, Any] | None:
    """Forward only snapshots from Calendar's managed default/Ops boundaries."""
    try:
        source_home = Path(os.environ.get("HERMES_HOME", "/home/brian/.hermes")).resolve()
    except OSError:
        return None
    profile = MANAGED_PROFILES.get(source_home)
    if profile is None:
        return None
    # Never accept caller-provided roots or free-form fields. The Ops runner
    # maps this fixed profile label to its own canonical inventory/output
    # boundary, and prompts/tokens/secrets must not cross the process boundary.
    return _redacted_job_snapshot(job, profile, source_home)


def _invoke(operation: str, job: dict[str, Any], **extra: object) -> None:
    if not _enabled() or not isinstance(job, dict):
        return
    snapshot = _snapshot(job)
    if snapshot is None:
        logger.warning("cron-calendar-sync hook %s ignored unmanaged source profile", operation)
        return
    payload: dict[str, object] = {"operation": operation, "job": snapshot}
    if operation == hooks.COMPLETE:
        output_file = _safe_output_file(extra.get("output_file"), snapshot)
        if output_file:
            payload["output_file"] = output_file
        duration = extra.get("duration_seconds")
        if isinstance(duration, (int, float)) and duration >= 0:
            payload["duration_seconds"] = duration
        payload["success"] = bool(extra.get("success"))
    env = os.environ.copy()
    # The runner deliberately owns the credential/policy root.  Do not inherit
    # a source profile as a substitute Calendar authority.
    env.pop("HERMES_HOME", None)
    try:
        result = subprocess.run(
            [sys.executable, str(OPS_RECONCILER), "--single-job"],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=90,
            env=env,
            check=False,
        )
        if result.returncode:
            # stderr can contain Calendar/API diagnostics, so record only the
            # deterministic process status at this hook boundary.
            logger.warning(
                "cron-calendar-sync hook %s Ops runner exited with status %d",
                operation,
                result.returncode,
            )
    except OSError as exc:
        logger.warning("cron-calendar-sync hook %s could not start Ops runner: %s", operation, exc)
    except subprocess.TimeoutExpired:
        logger.warning("cron-calendar-sync hook %s timed out", operation)


def on_create(job: dict, **_: object) -> None:
    _invoke(hooks.CREATE, job)


def on_update(job: dict, **_: object) -> None:
    _invoke(hooks.UPDATE, job)


def on_remove(job: dict, **_: object) -> None:
    # REMOVE receives the pre-delete snapshot from cron.jobs. Never look up the
    # job again: bounded jobs may already be absent from the registry.
    _invoke(hooks.REMOVE, job)


def on_complete(job: dict, **payload: object) -> None:
    _invoke(hooks.COMPLETE, job, **payload)


def register() -> None:
    hooks.register_hook(hooks.CREATE, on_create)
    hooks.register_hook(hooks.UPDATE, on_update)
    hooks.register_hook(hooks.REMOVE, on_remove)
    hooks.register_hook(hooks.COMPLETE, on_complete)
