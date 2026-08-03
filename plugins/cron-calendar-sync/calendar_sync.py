"""Thin lifecycle bridge to the Ops-owned cron Calendar reconciler.

Calendar policy, credentials, state, locking, and reconciliation live in the
Ops runner.  This bundled plugin only forwards immutable lifecycle snapshots;
that keeps every profile's hook on the same canonical Calendar projection.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from cron import hooks

logger = logging.getLogger(__name__)

OPS_HOME = Path("/home/brian/.hermes/profiles/ops").resolve()
# The runner ships with this plugin so a lifecycle hook never depends on an
# uncommitted worktree or a mutable profile-local implementation.
OPS_RUNNER = Path(__file__).resolve().with_name("ops_runner.py")
MANAGED_HOMES = {
    Path("/home/brian/.hermes").resolve(),
    OPS_HOME,
}
MANAGED_PROFILES = {
    Path("/home/brian/.hermes").resolve(): "default",
    OPS_HOME: "ops",
}


def _enabled() -> bool:
    try:
        from hermes_cli.config import load_config

        config = load_config()
        calendar = ((config.get("cron") or {}).get("calendar_sync") or {})
        return bool(calendar.get("enabled", True))
    except Exception:
        return False


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


def _snapshot(job: dict[str, Any]) -> dict[str, Any] | None:
    """Forward only snapshots from Calendar's managed default/Ops boundaries."""
    try:
        source_home = Path(os.environ.get("HERMES_HOME", "/home/brian/.hermes")).resolve()
    except OSError:
        return None
    profile = MANAGED_PROFILES.get(source_home)
    if profile is None:
        return None
    snapshot = dict(job)
    # Never accept a caller-provided source root. The runner maps this fixed
    # profile label to its own canonical inventory/output boundary.
    snapshot.pop("__profile_home", None)
    snapshot["__profile"] = profile
    return snapshot


def _invoke(operation: str, job: dict[str, Any], **extra: object) -> None:
    if not _enabled() or not OPS_RUNNER.is_file() or not isinstance(job, dict):
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
            [sys.executable, str(OPS_RUNNER), "--single-job"],
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
