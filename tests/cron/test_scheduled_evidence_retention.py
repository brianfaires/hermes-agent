"""Scheduled evidence survives unrelated traffic in the profile-local native ledger."""

from copy import deepcopy
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
    jobs.save_jobs(deepcopy(snapshot))
    provider = InProcessCronScheduler()
    claim = provider.claim_fire(weekly["id"])
    assert claim is not None
    assert provider.claim_fire(weekly["id"]) is None  # refused overlap cannot replace success
    running = ledger.mark_execution_running(claim["execution_id"])
    assert running["scheduled_instant"] == slot
    completed = ledger.finish_execution(running["id"], success=True, delivery_outcome="queued")

    unrelated = [_complete(ledger, "minute-watcher") for _ in range(1001)]

    assert ledger.get_execution(completed["id"]) == completed
    retained = []
    before = None
    while page := ledger.list_executions(limit=500, before_claimed_at=before):
        retained.extend(row["id"] for row in page)
        before = page[-1]["claimed_at"]
    assert set(retained) == {completed["id"], *(row["id"] for row in unrelated[-1000:])}

    # Restore the pre-claim jobs snapshot separately for each real dedup gate.
    jobs.save_jobs(deepcopy(snapshot))
    assert jobs.get_due_jobs() == []
    assert jobs.get_job(weekly["id"])["next_run_at"] != slot
    jobs.save_jobs(deepcopy(snapshot))
    assert provider.claim_fire(weekly["id"]) is None
    assert jobs.get_job(weekly["id"])["next_run_at"] != slot

    jobs.save_jobs(deepcopy(snapshot))
    manual = provider.claim_fire(weekly["id"], force=True)
    assert manual is not None
    assert ledger.get_execution(manual["execution_id"])["scheduled_instant"] is None
    ledger.finish_execution(manual["execution_id"], success=True)
    assert ledger.get_execution(completed["id"]) == completed

    later_slot = (now() - timedelta(minutes=1)).isoformat()
    snapshot[0]["next_run_at"] = later_slot
    jobs.save_jobs(deepcopy(snapshot))
    due = jobs.get_due_jobs()
    assert len(due) == 1 and due[0]["_scheduled_instant"] == later_slot
    later = provider.claim_fire(weekly["id"])
    assert later is not None
    assert ledger.get_execution(later["execution_id"])["scheduled_instant"] == later_slot
