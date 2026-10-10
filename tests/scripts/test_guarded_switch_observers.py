"""Offline adapter, all-profile observation and finite terminal-owner contracts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts/claude_release_switch'


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


g = module('guard', SCRIPTS / 'guarded_switch.py')
o = module('observer', SCRIPTS / 'terminal_observer.py')
i = module('idle', SCRIPTS / 'observe_idle.py')
h = module('harness', Path(__file__).with_name('test_guarded_switch.py'))


@pytest.fixture
def harness():
    t = h.GuardTests()
    t.setUp()
    yield t
    t.doCleanups()


def test_stop_over_sixty_within_service_bound_completes(harness):
    t = harness
    budgets = []

    def runner(argv, **kwargs):
        budget = kwargs['timeout']
        budgets.append(budget)
        if 73 > budget:
            raise subprocess.TimeoutExpired(argv, budget)
        return subprocess.CompletedProcess(argv, 0, b'')

    class Service(h.FixtureService):
        def stop(self):
            with patch.object(t.module.subprocess, 'run', side_effect=runner):
                t.module.Systemd(t.m, str(t.manifest), t.sha).stop()
            super().stop()

    result = t.module.execute('run', str(t.manifest), t.sha, Service)
    assert result == 'switched', f'73-second stop fenced with budgets={budgets}'
    assert t.service_data()['actions'] == ['stop', 'start']
    assert t.transaction()['phase'] == 'complete'


def test_stop_budget_shared_with_recovery_and_ordinary_budget(harness):
    t = harness
    budgets = []

    def runner(argv, **kwargs):
        timeout = kwargs['timeout']
        budgets.append((argv[2], timeout))
        elapsed = 73 if argv[2] == 'stop' else 1
        if elapsed > timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        return subprocess.CompletedProcess(argv, 0, b'')

    # Both forward and fallback use Systemd.stop, exercising the actual subprocess
    # budget boundary without 73 seconds of wall time or a real manager.
    class Service(h.FixtureService):
        def stop(self):
            with patch.object(t.module.subprocess, 'run', side_effect=runner):
                t.module.Systemd(t.m, str(t.manifest), t.sha).stop()
            super().stop()

    t.configure(stop_failure='no_effect')
    with pytest.raises(RuntimeError):
        t.module.execute('run', str(t.manifest), t.sha, Service)
    assert budgets == [('stop', 120), ('stop', 120)]
    with patch.object(g.subprocess, 'run', side_effect=runner):
        service = g.Systemd(t.m, str(t.manifest), t.sha)
        service.start()
        service.show(t.m['service'])
    assert budgets[-2:] == [('start', 60), ('show', 60)]


@pytest.mark.parametrize('value', ['infinity', '0', '91s', 'bad', None])
def test_unsupported_gateway_timeout_rejected_before_arm(harness, value):
    t = harness

    class Service(h.FixtureService):
        def pre_arm(self, current, gateway):
            service = t.module.Systemd(t.m, str(t.manifest), t.sha)
            with patch.object(service, 'show', return_value={
                'Id': t.m['service'], 'InvocationID': h.BASE, 'TimeoutStopUSec': value,
            }):
                return service.pre_arm(current, gateway)

    with pytest.raises(t.module.Refused):
        t.module.execute('run', str(t.manifest), t.sha, Service)
    assert not (t.state / 'transaction.json').exists()
    assert t.service_data()['actions'] == []


def test_real_stop_timeout_only_fenced_hook_can_recover(harness):
    t = harness

    class Service(h.FixtureService):
        def stop(self):
            super().stop()
            with patch.object(t.module.subprocess, 'run',
                              side_effect=subprocess.TimeoutExpired('systemctl', 120)):
                t.module.Systemd(t.m, str(t.manifest), t.sha).stop()

    assert t.module.execute('run', str(t.manifest), t.sha, Service) == 'awaiting_fence'
    assert t.transaction()['phase'] == 'awaiting_fence'
    assert t.service_data()['actions'] == ['stop']
    t.run_guard('recover', expected=1)
    t.recover()
    t.assert_good()


@pytest.fixture
def idle_home(tmp_path):
    home = tmp_path / 'home'
    (home / 'kanban/boards').mkdir(parents=True)
    (home / 'profiles/other/cron').mkdir(parents=True)
    (home / 'cron').mkdir()
    repo = tmp_path / 'repo'
    (repo / 'gateway').mkdir(parents=True)
    shutil.copyfile(ROOT / 'gateway/control_socket.py', repo / 'gateway/control_socket.py')
    proc = tmp_path / 'proc'
    proc.mkdir()
    raw = dict(pid=123, answering_pid=123, activity_writer_pid=123, code_sha='a'*40,
               gateway_state='running', activity_state='fresh', active_agents=0,
               activity_sampled_at=time.time())
    return home, repo, proc, raw


@pytest.mark.parametrize('busy', ['claim', 'execution', 'claim_file', 'board', 'agent', 'unknown', 'stale'])
def test_all_profile_busy_and_unknown(idle_home, busy):
    home, repo, proc, raw = idle_home
    assert i.observe(home, repo, 123, 'a'*40, lambda *args: raw, proc)
    cron = home / 'profiles/other/cron'
    if busy == 'claim':
        (cron / 'jobs.json').write_text(json.dumps([{'fire_claim': {'id': 'busy'}}]))
    elif busy == 'execution':
        with sqlite3.connect(cron / 'executions.db') as db:
            db.execute('CREATE TABLE executions (job_id TEXT, status TEXT)')
            db.execute("INSERT INTO executions VALUES ('busy', 'running')")
    elif busy == 'claim_file':
        (cron / '.exec-busy').touch()
    elif busy == 'board':
        folder = home / 'kanban/boards/test'
        folder.mkdir()
        with sqlite3.connect(folder / 'kanban.db') as db:
            db.execute('CREATE TABLE tasks (id TEXT, status TEXT, worker_pid INTEGER)')
            db.execute("INSERT INTO tasks VALUES ('busy', 'running', 42)")
    elif busy == 'agent':
        raw['active_agents'] = 1
    elif busy == 'unknown':
        raw = None
    else:
        raw['activity_sampled_at'] -= 10
    assert not i.observe(home, repo, 123, 'a'*40, lambda *args: raw, proc)


def test_adapter_executes_pinned_idle_script_over_real_socket(idle_home, tmp_path):
    home, repo, proc, raw = idle_home
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    script = private / 'observe_idle.py'
    shutil.copyfile(SCRIPTS / script.name, script)
    script.chmod(0o600)
    m = dict(service='test.service', repo=str(repo), interpreter=str(Path(sys.executable).resolve()),
             idle_observer=dict(path=str(script), home=str(home), sha256=g.digest(script.read_bytes())))
    service = g.Systemd(m, '', '')
    # Use the real control module's pointer support for long temporary paths.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        sockpath = tmp_path / 's'
        server.bind(str(sockpath))
        server.listen()
        (home / 'gateway.sock.path').write_text(str(sockpath))

        def respond():
            conn, _ = server.accept()
            with conn:
                conn.recv(4096)
                conn.sendall((json.dumps({'ok': True, 'result': raw}) + '\n').encode())

        thread = threading.Thread(target=respond, daemon=True)
        thread.start()
        with patch.object(service, 'show', return_value={'Id': 'test.service', 'InvocationID': h.BASE,
                                                       'TimeoutStopUSec': '1min 30s'}):
            assert service.pre_arm({'sha': 'a'*40}, {'invocation': h.BASE, 'pid': 123})
        thread.join(timeout=3)
        assert not thread.is_alive()
    script.write_text('raise RuntimeError("unreviewed")')
    with patch.object(service, 'show', return_value={'Id': 'test.service', 'InvocationID': h.BASE,
                                                   'TimeoutStopUSec': '90s'}):
        with pytest.raises(g.Refused, match='changed'):
            service.pre_arm({'sha': 'a'*40}, {'invocation': h.BASE, 'pid': 123})


@pytest.fixture
def bound(tmp_path):
    directory = tmp_path / 'observer'
    directory.mkdir(mode=0o700)
    (directory / 'lock').touch(mode=0o600)
    journal = tmp_path / 'journal.json'
    h.write_json(journal, dict(manifest='a'*64, executor_invocation=h.EXECUTOR,
                               phase='complete', outcome='recovered'))
    b = dict(executor='fixture.service', invocation=h.EXECUTOR, manifest_sha256='a'*64,
             journal=str(journal), owner='test controller', state_dir=str(directory),
             expires_at=2000, max_attempts=3, retry_seconds=60)
    filename = tmp_path / 'binding.json'
    h.write_json(filename, b)
    sha = g.digest(filename.read_bytes())
    terminal = dict(Id=b['executor'], InvocationID=b['invocation'], LoadState='loaded',
                    MainPID='0', ControlPID='0', ActiveState='failed')
    return filename, sha, b, terminal


def test_live_executor_and_hook_silent_terminal_ack_dedup(bound):
    filename, sha, b, terminal = bound
    for live in ({'MainPID': '42', 'ActiveState': 'active'},
                 {'ControlPID': '43', 'ActiveState': 'deactivating'},
                 {'ActiveState': 'deactivating'}):
        assert o.run(filename, sha, now=1000, inspect=lambda _: {**terminal, **live}) == ''
    notice = json.loads(o.run(filename, sha, now=1000, inspect=lambda _: terminal))
    assert notice['observer'] == 'terminal_recovered'
    assert o.run(filename, sha, now=1001, inspect=lambda _: terminal) == ''
    retry = json.loads(o.run(filename, sha, now=1060, inspect=lambda _: terminal))
    assert retry['token'] == notice['token']
    with pytest.raises(ValueError):
        o.run(filename, sha, ack=notice['token'], receipt='')
    with pytest.raises(ValueError):
        o.run(filename, sha, ack='stale', receipt='message-1')
    o.run(filename, sha, ack=notice['token'], receipt='controller receipt message-1')
    assert o.run(filename, sha, now=3000, inspect=lambda _: terminal) == ''
    assert json.loads((Path(b['state_dir']) / 'observer.json').read_text())['status'] == 'acknowledged'


def test_interrupted_attempt_has_later_opportunity_and_bounded_failure(bound):
    filename, sha, b, terminal = bound
    save = o.save

    def interrupted(*args):
        save(*args)
        raise KeyboardInterrupt  # after durable attempt, before stdout/delivery

    with patch.object(o, 'save', side_effect=interrupted), pytest.raises(KeyboardInterrupt):
        o.run(filename, sha, now=1000, inspect=lambda _: terminal)
    assert o.run(filename, sha, now=1060, inspect=lambda _: terminal)
    last = json.loads(o.run(filename, sha, now=1120, inspect=lambda _: terminal))
    assert last['status'] == 'unacknowledged_exhausted'
    assert o.run(filename, sha, now=1180, inspect=lambda _: terminal) == ''
    assert json.loads((Path(b['state_dir']) / 'observer.json').read_text())['status'] != 'acknowledged'


@pytest.mark.parametrize('case,expected', [('expiry', 'expired_unacknowledged'),
                                          ('wrong_invocation', 'observation_failed'),
                                          ('hook_failed', 'terminal_recovery_required'),
                                          ('journal_mismatch', 'observation_failed')])
def test_expiry_and_owned_failures(bound, case, expected):
    filename, sha, b, terminal = bound
    now = 2000 if case == 'expiry' else 1000
    if case == 'wrong_invocation':
        terminal['InvocationID'] = 'f'*32
    if case in ('hook_failed', 'journal_mismatch'):
        j = json.loads(Path(b['journal']).read_text())
        j['phase'] = 'recovery_required'
        if case == 'journal_mismatch':
            j['manifest'] = 'f'*64
        h.write_json(Path(b['journal']), j)
    notice = json.loads(o.run(filename, sha, now=now, inspect=lambda _: terminal))
    assert notice['observer'] == expected
    assert notice['status'] == 'pending_ack'


def test_existing_cron_interruption_keeps_recurring_opportunity(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    from cron.jobs import create_job, get_job, mark_job_run
    job = create_job(prompt='', schedule='every 1m', repeat=30, script='observer.sh',
                     no_agent=True, deliver='local')
    mark_job_run(job['id'], success=False, error='interrupted')
    after = get_job(job['id'])
    assert after['enabled'] and after['next_run_at']
    assert after['repeat']['completed'] < after['repeat']['times']


def test_existing_no_agent_executes_observer_without_acknowledging(bound, tmp_path, monkeypatch):
    filename, sha, b, terminal = bound
    home = tmp_path / 'cron-home'
    scripts = home / 'scripts'
    scripts.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    script = scripts / 'observe.py'
    script.write_text(
        'import importlib.util\n'
        f'spec = importlib.util.spec_from_file_location("observer", {str(SCRIPTS / "terminal_observer.py")!r})\n'
        'observer = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(observer)\n'
        f'output = observer.run({str(filename)!r}, {sha!r}, now=1000, inspect=lambda _: {terminal!r})\n'
        'if output: print(output)\n'
    )
    from cron.scheduler import run_job
    job = dict(id='offline-observer', name='offline observer', script='observe.py', no_agent=True)
    success, _, response, error = run_job(job)
    assert success and error is None
    assert json.loads(response)['observer'] == 'terminal_recovered'
    state = json.loads((Path(b['state_dir']) / 'observer.json').read_text())
    assert state['status'] == 'pending_ack'
    success, _, response, error = run_job(job)
    assert success and error is None and response == '[SILENT]'


@pytest.mark.parametrize('live', [{'MainPID': '42', 'ActiveState': 'active'},
                                  {'ControlPID': '43', 'ActiveState': 'deactivating'}])
def test_expiry_persisted_while_owner_live_without_output(bound, live):
    filename, sha, b, terminal = bound
    assert o.run(filename, sha, now=2000, inspect=lambda _: {**terminal, **live}) == ''
    state = json.loads((Path(b['state_dir']) / 'observer.json').read_text())
    assert state['binding'] == sha and state['expired'] is True
    assert state['expiry_blocker'] == 'owner_live'
    assert state['attempts'] == 0 and 'token' not in state and 'receipt' not in state
    notice = json.loads(o.run(filename, sha, now=2060, inspect=lambda _: terminal))
    assert notice['observer'] == 'expired_unacknowledged'


@pytest.mark.parametrize('unknown', [False, True])
def test_expiry_persisted_after_exhaustion_without_more_attempts(bound, unknown):
    filename, sha, b, terminal = bound
    notices = [json.loads(o.run(filename, sha, now=now, inspect=lambda _: terminal))
               for now in (1000, 1060, 1120)]
    inspect = lambda _: {**terminal, 'InvocationID': 'f'*32} if unknown else terminal
    assert o.run(filename, sha, now=2000, inspect=inspect) == ''
    state = json.loads((Path(b['state_dir']) / 'observer.json').read_text())
    assert state['expired'] is True
    assert state['expiry_blocker'] == ('terminality_unverified' if unknown else 'unacknowledged_terminal')
    assert state['attempts'] == b['max_attempts'] and state['status'] == 'unacknowledged_exhausted'
    assert state['token'] == notices[0]['token'] and state['kind'] == 'terminal_recovered'
    assert 'receipt' not in state


def test_failure_receipt_does_not_become_terminal_result(bound):
    filename, sha, b, terminal = bound
    notice = json.loads(o.run(filename, sha, now=1000,
                              inspect=lambda _: {**terminal, 'InvocationID': 'f'*32}))
    assert notice['observer'] == 'observation_failed'
    assert 'takeover' in notice['request'] and 'hook' in notice['request']
    o.run(filename, sha, ack=notice['token'], receipt='owner received failure-1')
    assert o.run(filename, sha, now=1060, inspect=lambda _: terminal) == ''
    state = json.loads((Path(b['state_dir']) / 'observer.json').read_text())
    assert state['kind'] == 'observation_failed' and state['status'] == 'acknowledged'
    assert 'terminal_recovered' not in state.values()


def test_failed_delivery_then_silent_scheduler_completion_has_no_receipt(bound, tmp_path, monkeypatch):
    filename, sha, b, terminal = bound
    home = tmp_path / 'delivery-home'
    scripts = home / 'scripts'
    scripts.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    script = scripts / 'observe.py'
    script.write_text(
        'import importlib.util\n'
        f'spec = importlib.util.spec_from_file_location("observer", {str(SCRIPTS / "terminal_observer.py")!r})\n'
        'observer = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(observer)\n'
        f'output = observer.run({str(filename)!r}, {sha!r}, now=1000, inspect=lambda _: {terminal!r})\n'
        'if output: print(output)\n'
    )
    from cron.jobs import create_job, get_job
    from cron.executions import iter_execution_results
    import cron.scheduler as scheduler
    job = create_job(prompt='', schedule='every 1m', repeat=2, script='observe.py',
                     no_agent=True, deliver='telegram')
    attempts = []

    def failed_delivery(job, content, **kwargs):
        attempts.append(content)
        return 'offline fixture: delivery failed'

    # Only the outbound delivery boundary is replaced. Script subprocess,
    # output saving, job bookkeeping and execution ledger are all real.
    monkeypatch.setattr(scheduler, '_deliver_result', failed_delivery)
    finish = scheduler.finish_execution
    outcomes = []

    def record_finish(*args, **kwargs):
        outcomes.append(kwargs.get('delivery_outcome'))
        return finish(*args, **kwargs)

    monkeypatch.setattr(scheduler, 'finish_execution', record_finish)
    assert scheduler.run_one_job(job)
    first = get_job(job['id'])
    assert first['last_delivery_error'] == 'offline fixture: delivery failed'
    assert scheduler.run_one_job(first)
    final = get_job(job['id'])
    assert not final['enabled'] and final['last_status'] == 'ok'
    assert final['last_delivery_error'] is None and len(attempts) == 1
    assert outcomes == ['failed', 'suppressed']
    history = [entry for entry in iter_execution_results() if entry['job_id'] == job['id']]
    assert len(history) == 2
    responses = [entry['final_response'] for entry in history]
    assert '[SILENT]' in responses
    assert any('terminal_recovered' in response for response in responses)
    state = json.loads((Path(b['state_dir']) / 'observer.json').read_text())
    assert state['status'] == 'pending_ack' and 'receipt' not in state
