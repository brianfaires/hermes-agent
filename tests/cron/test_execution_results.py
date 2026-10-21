"""Terminal facts survive operational-history pruning without retaining transcripts."""
import json

from cron import executions, scheduler
from gateway.run import _profile_runtime_scope


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
