"""Pre-agent monitor admission and reuse of a shared script's verified output."""
from __future__ import annotations


def apply_monitor_gate(
    job: dict, job_id: str, job_name: str, extra_prompt: str | None,
) -> tuple[tuple | None, str | None, str | None, tuple[bool, str] | None]:
    """Return an early result, user prompt, runtime context, and optional cached script output."""
    from cron import scheduler
    from cron.monitor import check_monitor, job_has_monitor

    if not job_has_monitor(job):
        return None, extra_prompt, None, None
    outcome = check_monitor(job)
    now = scheduler._hermes_now().strftime("%Y-%m-%d %H:%M:%S")
    header = scheduler._job_doc_header(job_name, job_id, now, "monitor")
    if not outcome.ok:
        scheduler.logger.error("Job '%s': monitor source failed: %s", job_id, outcome.error)
        alert = f"⚠ Cron monitor '{job_name}' source failed\n\n{outcome.error}\n\nTime: {now}"
        result = (False, f"{header}**Status:** monitor source failed\n\n{outcome.error}\n",
                  alert, outcome.error)
        return result, extra_prompt, None, None
    if not outcome.changed:
        scheduler.logger.info("Job '%s': monitor output unchanged or gate closed — suppressing agent run", job_id)
        result = (True, f"{header}**Status:** no_change (agent run suppressed)\n",
                  scheduler.SILENT_MARKER, None)
        return result, extra_prompt, None, None
    cached = (True, outcome.source_output) if outcome.source_output is not None else None
    # Runtime monitor data remains separate from strict-scanned user-authored extra_prompt.
    return None, extra_prompt, outcome.context_block, cached
