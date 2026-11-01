"""Bind the owned fire's plugin identity and durable result labels before execution."""
from __future__ import annotations

import os


def start_owned_run(job, execution_id):
    from cron import scheduler
    from cron.execution_identity import enter_cron_execution
    from cron.execution_results import capture_execution_job

    if os.environ.get('_HERMES_CRON_EXTERNAL_WORKER') == execution_id:
        record = scheduler.get_execution(execution_id)
    elif (record := scheduler.mark_execution_running(execution_id)) is None:
        return None
    capture_execution_job(execution_id, job)
    return enter_cron_execution(job, execution_id, record or {})
