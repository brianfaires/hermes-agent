"""Native cross-board handoffs retain exact runs, actual executor facts and waits."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
from threading import Barrier

import pytest

import run_agent
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_notify as notify
from hermes_cli import kanban_db_workspace as workspace


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / '.hermes'
    root.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(root))
    for key in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_TASK', 'HERMES_DELEGATED_CHILD_CONTEXT'):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    return root


def test_concurrent_recheck_creates_one_handoff_card(home, monkeypatch):
    barrier = Barrier(2)
    native_id = kb._new_task_id

    def simultaneous_id():
        value = native_id()
        barrier.wait(timeout=20)
        return value

    monkeypatch.setattr(kb, '_new_task_id', simultaneous_id)

    def request(index):
        with kbc.connect_closing() as conn:
            return kb.create_task(conn, title=f'Request {index}',
                                  idempotency_key='handoff:ops:source-card:run-1')

    with ThreadPoolExecutor(max_workers=2) as pool:
        identities = list(pool.map(request, range(2)))
    assert len(set(identities)) == 1
    with kbc.connect_closing() as conn:
        assert len(kb.list_tasks(conn)) == 1
        assert [event.kind for event in kb.list_events(conn, identities[0])] == ['created']


def _executor(result: Path, counter: Path, exitcode: int):
    script = (
        "from pathlib import Path; import sys; "
        f"counter=Path({str(counter)!r}); counter.write_text(str(int(counter.read_text())+1) if counter.exists() else '1'); "
        "print('ready', flush=True); sys.stdin.readline(); "
        f"Path({str(result)!r}).write_text('actual executor result: {exitcode}'); "
        f"sys.exit({exitcode})"
    )
    process = subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline() == 'ready\n'
    return process


@pytest.mark.parametrize('exitcode', [0, 3])
def test_exact_board_card_run_and_real_executor_receipts_survive_recheck(home, tmp_path, exitcode):
    kb.create_board('ops')
    kb.create_board('build')
    with kb.scoped_current_board('ops'), kbc.connect_closing(board='ops') as conn:
        owner = kb.create_task(conn, title='Wait for external deliverable', assignee='operator')
        owner_run = kb.claim_task(conn, owner).current_run_id
    key = f'handoff:ops:{owner}:{owner_run}'
    with kb.scoped_current_board('build'), kbc.connect_closing(board='build') as conn:
        target = kb.create_task(conn, title='External executor', assignee='executor', idempotency_key=key)
        target_task = kb.claim_task(conn, target)
        target_run = target_task.current_run_id
        target_workspace = workspace.resolve_workspace(target_task, board='build')
        workspace.set_workspace_path(conn, target, target_workspace)
        notify.add_notify_sub(conn, task_id=target, platform='telegram', chat_id='owner-chat',
                               notifier_profile='operator', delivery_mode='notify+wake')
    identity = {'owner': {'board': 'ops', 'card': owner, 'run': owner_run},
                'target': {'board': 'build', 'card': target, 'run': target_run},
                'execution_id': f'fixture-external-{owner_run}-{target_run}'}
    result = target_workspace / 'executor-result.txt'
    counter = tmp_path / 'actual-launch-count'
    process = _executor(result, counter, exitcode)
    try:
        with kb.scoped_current_board('build'), kbc.connect_closing(board='build') as conn:
            assert dispatch.adopt_worker_pid(conn, target, target_run, process.pid)
            launched = {**identity, 'state': 'launched', 'pid': process.pid,
                        'started_at': dispatch._process_fingerprint(process.pid)}
            assert launched['started_at']
            kb.add_comment(conn, target, 'executor', json.dumps(launched))
        with kb.scoped_current_board('ops'), kbc.connect_closing(board='ops') as conn:
            kb.store_attachment_bytes(conn, owner, 'external-wait.json', json.dumps(launched).encode(), board='ops')
            assert kb.block_task(conn, owner, expected_run_id=owner_run, kind='transient',
                                  reason=f'Await {identity["target"]}; execution {identity["execution_id"]}')
        process.communicate('\n', timeout=10)
        assert process.returncode == exitcode
        receipt = {**launched, 'state': 'completed', 'returncode': process.returncode}
        with kb.scoped_current_board('build'), kbc.connect_closing(board='build') as conn:
            kb.store_attachment_bytes(conn, target, 'external-result.json', json.dumps(receipt).encode(), board='build')
            if exitcode == 0:
                assert kb.complete_task(conn, target, expected_run_id=target_run,
                                         summary='Actual executor deliverable verified.',
                                         metadata={'external_execution': receipt, 'artifacts': [str(result)]})
            else:
                assert kb.block_task(conn, target, expected_run_id=target_run, kind='needs_input',
                                      reason=f'Executor {identity["execution_id"]} exited {exitcode}; owner attention required')
        for _ in range(3):
            with kb.scoped_current_board('build'), kbc.connect_closing(board='build') as conn:
                assert kb.create_task(conn, title='Repeated request', idempotency_key=key) == target
                run = kb.get_run(conn, target_run)
                assert run.task_id == target and len(kb.list_runs(conn, target)) == 1
                artifacts = kb.list_attachments(conn, target)
                stored = next(item for item in artifacts if item.filename == 'external-result.json')
                assert json.loads(Path(stored.stored_path).read_text()) == receipt
                assert all(Path(item.stored_path).is_file() for item in artifacts)
                if exitcode == 0:
                    declared = next(item for item in artifacts if item.filename == 'executor-result.txt')
                    assert Path(declared.stored_path).read_text() == 'actual executor result: 0'
                assert notify.list_notify_subs(conn, target)[0]['notifier_profile'] == 'operator'
                assert kb.get_task(conn, target).status == ('done' if exitcode == 0 else 'blocked')
            with kb.scoped_current_board('ops'), kbc.connect_closing(board='ops') as conn:
                assert kb.get_task(conn, owner).status == 'blocked'
                assert kb.get_run(conn, owner_run).outcome == 'blocked'
        assert counter.read_text() == '1'
        if exitcode == 0:
            assert not result.exists(), 'native cleanup follows durable artifact staging'
            with kb.scoped_current_board('ops'), kbc.connect_closing(board='ops') as conn:
                assert kb.unblock_task(conn, owner)
                resumed = kb.claim_task(conn, owner)
                assert resumed and resumed.current_run_id != owner_run
    finally:
        if process.poll() is None:
            process.communicate('\n', timeout=10)
        else:
            process.wait(timeout=10)
