"""Terminal facts survive operational-history pruning without retaining transcripts."""
import json

import pytest

from cron import executions, scheduler
from gateway.run import _profile_runtime_scope


@pytest.mark.parametrize("foreign_owner", [False, True], ids=["unadopted", "foreign"])
def test_receipt_capture_refuses_unowned_execution(tmp_path, monkeypatch, foreign_owner):
    from cron.execution_results import capture_execution_job, iter_execution_results

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "executions.db")
    row = executions.create_execution("owner-fence", source="fixture")
    if foreign_owner:
        assert executions.mark_execution_running(row["id"])
        monkeypatch.setattr(executions, "_PROCESS_ID", "another-worker")
    with pytest.raises(RuntimeError, match="no longer owned"):
        capture_execution_job(row["id"], {"name": "Unowned result"})
    assert list(iter_execution_results()) == []
    assert executions.get_execution(row["id"])["status"] == ("running" if foreign_owner else "claimed")


def test_receipts_are_atomic_immutable_and_profile_local(tmp_path, monkeypatch):
    from cron.execution_results import capture_execution_job, iter_execution_results

    monkeypatch.setattr(executions, 'MAX_TERMINAL_EXECUTIONS', 0)
    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', None)
    owner = tmp_path / 'owner'
    other = tmp_path / 'other'
    with _profile_runtime_scope(owner):
        row = executions.create_execution('one-shot', source='direct')
        assert executions.mark_execution_running(row['id'])
        capture_execution_job(row['id'], dict(name='Task label', prompt='PRIVATE PROMPT',
            schedule={'kind': 'once', 'run_at': '2026-10-19T00:00:00+00:00'}))
        assert executions.finish_execution(row['id'], success=False, error='observed failure',
            final_response='Final answer', delivery_outcome='failed')
        assert executions.list_executions() == []
        assert executions.finish_execution(row['id'], success=True, final_response='overwrite') is None
        receipts = list(iter_execution_results(page_size=1))
        assert len(receipts) == 1
        assert receipts[0]['final_response'] == 'Final answer'
        assert receipts[0]['job_name'] == 'Task label'
        assert receipts[0]['status'] == 'failed'
        assert receipts[0]['delivery_outcome'] == 'failed'
        assert 'PRIVATE PROMPT' not in json.dumps(receipts)
    with _profile_runtime_scope(other):
        assert list(iter_execution_results()) == []


def test_scheduler_retains_final_answer_after_one_shot_removal(tmp_path, monkeypatch):
    from cron.execution_results import iter_execution_results
    from cron.jobs import create_job, remove_job, use_cron_store

    home = tmp_path / 'home'
    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', None)
    monkeypatch.setattr(executions, 'MAX_TERMINAL_EXECUTIONS', 1)

    def run(job, **kwargs):
        remove_job(job['id'])
        return True, 'FULL TRANSCRIPT NOT A RESULT', 'Final answer for ' + job['name'], None

    monkeypatch.setattr(scheduler, 'run_job', run)
    with _profile_runtime_scope(home), use_cron_store(home):
        for number in range(3):
            job = create_job(prompt='PRIVATE PROMPT', schedule='in 1m',
                             name=f'Removed {number}', deliver='local')
            assert scheduler.run_one_job(job)
        rows = list(iter_execution_results(page_size=1))
        assert len(rows) == 3
        assert all(row['final_response'] == 'Final answer for ' + row['job_name'] for row in rows)
        assert 'FULL TRANSCRIPT' not in json.dumps(rows)
        assert 'PRIVATE PROMPT' not in json.dumps(rows)


@pytest.mark.parametrize('history_limit', [0, 1000])
def test_verified_recovery_and_delivery_settlement_keep_the_final_native_facts(tmp_path, monkeypatch, history_limit):
    from cron.execution_results import capture_execution_job, iter_execution_results

    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', None)
    monkeypatch.setattr(executions, 'MAX_TERMINAL_EXECUTIONS', history_limit)
    with _profile_runtime_scope(tmp_path / 'owner'):
        row = executions.create_execution('recovered-job', source='direct')
        assert executions.mark_execution_running(row['id'])
        capture_execution_job(row['id'], {'name': 'Recovered label', 'schedule': {}})
        monkeypatch.setattr(executions, '_PROCESS_ID', 'replacement-owner')
        monkeypatch.setattr(executions, '_owner_is_live', lambda *args: False)
        assert executions.recover_interrupted_executions() == 1
        assert next(iter_execution_results())['status'] == 'unknown'
        assert executions.recover_receipted_execution(
            row['id'], 'different-job', success=True, final_response='wrong identity') is None
        assert executions.recover_receipted_execution(
            row['id'], 'recovered-job', success=True, final_response='Verified final answer',
            delivery_outcome='queued')
        receipt, = iter_execution_results()
        assert receipt['status'] == 'completed'
        assert receipt['final_response'] == 'Verified final answer'
        assert executions.settle_delivery_outcome(row['id'], 'delivered')
        assert not executions.settle_delivery_outcome(row['id'], 'failed')
        receipt, = iter_execution_results()
        assert receipt['delivery_outcome'] == 'delivered'
        assert receipt['final_response'] == 'Verified final answer'
        assert executions.recover_receipted_execution(
            row['id'], 'recovered-job', success=False, final_response='late overwrite') is None


def test_terminal_result_retained_on_pre_default_snapshot_schema(tmp_path, monkeypatch):
    """#175: tables created before ``snapshot`` had ``DEFAULT '{}'`` must still retain results."""
    import sqlite3

    from cron.execution_results import iter_execution_results

    db = tmp_path / "executions.db"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", db)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE execution_results (execution_id TEXT PRIMARY KEY, "
                 "snapshot TEXT NOT NULL, terminal_record TEXT)")
    conn.commit()
    conn.close()
    row = executions.create_execution("old-schema", source="fixture")
    assert executions.mark_execution_running(row["id"])
    assert executions.finish_execution(row["id"], success=True, final_response="Kept")
    results = list(iter_execution_results())
    assert [r["final_response"] for r in results] == ["Kept"]
