"""Never replace an owned worker while it survives a reclaim attempt."""
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    with kbc.connect_closing(tmp_path / 'kanban.db') as conn:
        yield conn


@pytest.fixture
def worker():
    process = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.readline()'],
                               stdin=subprocess.PIPE, text=True)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.communicate('\n', timeout=10)
        else:
            process.wait(timeout=10)


def _running(board, worker):
    task = kb.create_task(board, title='Owned worker', assignee='worker', max_runtime_seconds=1)
    claimed = kb.claim_task(board, task)
    dispatch._set_worker_pid(board, task, worker.pid)
    old = int(time.time()) - 10000
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET started_at=?, last_heartbeat_at=?, claim_expires=? WHERE id=?',
                      (old, old, old, task))
        board.execute('UPDATE task_runs SET started_at=? WHERE id=?', (old, claimed.current_run_id))
    return task, kb.get_task(board, task)


@pytest.mark.parametrize('reclaim', ['runtime', 'heartbeat'])
def test_survivor_retains_exact_claim_and_run(board, worker, monkeypatch, reclaim):
    task, before = _running(board, worker)
    signals = []
    monkeypatch.setattr(dispatch, '_poll_worker_exit', lambda *a: False)
    signal_fn = lambda pid, sig: signals.append((pid, sig))
    if reclaim == 'runtime':
        assert dispatch.enforce_max_runtime(board, signal_fn=signal_fn) == []
    else:
        assert dispatch.detect_stale_running(board, stale_timeout_seconds=1, signal_fn=signal_fn) == []
    assert worker.poll() is None
    after = kb.get_task(board, task)
    assert (after.status, after.claim_lock, after.current_run_id, after.worker_pid) == (
        'running', before.claim_lock, before.current_run_id, worker.pid)
    assert after.consecutive_failures == before.consecutive_failures
    assert kb.claim_task(board, task) is None
    assert len(signals) == 2
    events = kb.list_events(board, task)
    assert any(event.kind == 'reclaim_deferred' for event in events)
    assert not any(event.kind in {'timed_out', 'stale', 'gave_up'} for event in events)


def test_gone_worker_can_release_runtime_claim(board, worker):
    task, before = _running(board, worker)
    worker.communicate('\n', timeout=10)
    assert dispatch.enforce_max_runtime(board) == [task]
    after = kb.get_task(board, task)
    assert after.status == 'ready'
    assert after.claim_lock is None and after.current_run_id is None
    assert after.consecutive_failures == 1
    run = board.execute('SELECT outcome FROM task_runs WHERE id=?', (before.current_run_id,)).fetchone()
    assert run['outcome'] == 'timed_out'
