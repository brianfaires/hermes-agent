"""Scheduled evidence survives unrelated traffic in the profile-local native ledger."""

from datetime import timedelta

import pytest


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    from cron import executions, jobs

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    with jobs.use_cron_store(tmp_path / "cron"):
        yield executions


def _complete(ledger, job_id, *, source="builtin", instant=None, **outcome):
    row = ledger.create_execution(job_id, source=source, scheduled_instant=instant)
    ledger.mark_execution_running(row["id"])
    return ledger.finish_execution(row["id"], success=True, **outcome)


def test_weekly_native_claim_survives_more_than_1000_unrelated_completions(ledger):
    from cron import jobs
    from cron.scheduler_provider import InProcessCronScheduler
    from hermes_time import now

    weekly = jobs.create_job(prompt="weekly evidence", schedule="every 7d")
    snapshot = jobs.load_jobs()
    slot = (now() - timedelta(minutes=2)).isoformat()
    snapshot[0]["next_run_at"] = slot
    jobs.save_jobs(snapshot)
    claim = InProcessCronScheduler().claim_fire(weekly["id"])
    assert claim is not None
    running = ledger.mark_execution_running(claim["execution_id"])
    assert running["scheduled_instant"] == slot
    completed = ledger.finish_execution(running["id"], success=True, delivery_outcome="queued")

    for _ in range(1001):
        _complete(ledger, "minute-watcher")

    assert ledger.get_execution(completed["id"]) == completed
