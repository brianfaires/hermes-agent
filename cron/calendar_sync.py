"""Attach cron run output to its Google Calendar instance (opt-in, Brian-local).

Registers a COMPLETE cron-hook callback (see :mod:`cron.hooks`) that appends a
finished run's saved output to the concrete Calendar event instance for that run.

Best-effort: if the local sync script is absent it no-ops, and any failure is
logged only — it never affects cron run status or delivery. The actual calendar
mutation lives in ``~/.hermes/scripts/cron_calendar_recurring_sync.py`` so this
module stays free of Google API dependencies.

This replaces the previous inline ``_attach_cron_output_to_calendar`` edit in
``cron/scheduler.py``: the behavior now rides the intended lifecycle-hook
pathway instead of growing the scheduler.
"""

import importlib.util
import logging
from pathlib import Path
from typing import Any, Optional

import cron.hooks as cron_hooks
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_registered = False


def attach_output_to_calendar(
    *,
    job: Optional[dict] = None,
    output_file: Optional[str] = None,
    **_ignored: Any,
) -> None:
    """COMPLETE-hook callback: append this run's output to its Calendar instance.

    Accepts ``**_ignored`` so new COMPLETE payload fields never break it.
    """
    if not isinstance(job, dict) or not output_file:
        return
    script_path = get_hermes_home() / "scripts" / "cron_calendar_recurring_sync.py"
    if not script_path.exists():
        return
    try:
        spec = importlib.util.spec_from_file_location(
            "hermes_cron_calendar_recurring_sync", script_path
        )
        if spec is None or spec.loader is None:
            logger.warning(
                "Cron calendar output attach skipped: could not load %s", script_path
            )
            return
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        attach = getattr(module, "attach_output_to_calendar_event", None)
        if attach is None:
            logger.warning(
                "Cron calendar output attach skipped: %s has no "
                "attach_output_to_calendar_event",
                script_path,
            )
            return
        result = attach(job, Path(output_file))
        if isinstance(result, dict) and result.get("errors"):
            logger.warning(
                "Cron calendar output attach reported errors for job %s: %s",
                job.get("id"),
                result.get("error_messages"),
            )
    except Exception as exc:
        logger.warning(
            "Cron calendar output attach failed for job %s: %s", job.get("id"), exc
        )


def register() -> None:
    """Idempotently register the COMPLETE-hook calendar callback."""
    global _registered
    if _registered:
        return
    cron_hooks.register_hook(cron_hooks.COMPLETE, attach_output_to_calendar)
    _registered = True
