"""An unattended owner accepts a retained handoff through the native cron/dispatch boundary.

Regression for #29. The inference boundary is replaced; scripts, board state,
worker process, launch receipts and subsequent quiet checks are real and isolated.
"""
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

from cron.jobs import create_job, get_job, pause_job, use_cron_store
from cron.scheduler import SILENT_MARKER, run_job
from cron.scheduler_provider import _profile_cron_scope
from hermes_cli import kanban_continuation as continuation
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_state import SessionDB


@pytest.fixture(params=['default', 'coordinator'])
def workflow(tmp_path, monkeypatch, request):
    root = tmp_path / '.hermes'
    root.mkdir()
    profile = request.param
    home = root if profile == 'default' else root / 'profiles' / profile
    (home / 'scripts').mkdir(parents=True)
    (root / 'profiles' / 'ang').mkdir(parents=True)
    (root / 'profiles' / 'ang' / 'config.yaml').write_text('{}\n')
    (home / 'config.yaml').write_text('model: {default: chosen-model, provider: custom}\n')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_PROFILE', raising=False)
    repo = Path(__file__).resolve().parents[2]
    with _profile_cron_scope(home), use_cron_store(home), kbc.connect_closing() as conn:
        job = create_job(prompt='Accept only the admitted authorized handoff and verify its effect.',
                         schedule='every 5m', script='gate.py', monitor_script='gate.py',
                         interpreter=sys.executable,
                         workdir=str(repo),
                         model='chosen-model', provider='custom',
                         attach_to_session=False, deliver='local')
        calls = home / 'gate-calls.txt'
        (home / 'scripts' / 'gate.py').write_text(
            'import sys\n'
            f'sys.path.insert(0, {str(repo)!r})\n'
            'from pathlib import Path\n'
            f'p = Path({str(calls)!r})\n'
            'p.write_text(str(int(p.read_text()) + 1) if p.exists() else "1")\n'
            'from hermes_cli.kanban_continuation import emit_gate\n'
            f'emit_gate(job_id={job["id"]!r}, profile={profile!r})\n')
        cards = [kb.create_task(conn, title=title, assignee='ang')
                 for title in ('Approved implementation finished', 'Approved review handoff')]
        for card in cards:
            continuation.register_standing_coordination(
                conn, card=card, profile=profile, job_id=job['id'],
                authority_actor='Brian', authority_reference=f'approved:{card}')
        yield home, conn, job, *cards, profile


def recording_owner(monkeypatch, action):
    """Replace only model/provider resolution; the accepted action uses real lifecycle APIs."""
    from cron import scheduler
    from hermes_cli import runtime_provider

    observed = []

    class Owner:
        def __init__(self, **kwargs):
            assert kwargs['model'] == 'chosen-model'
            assert kwargs['provider'] == 'custom'

        def run_conversation(self, prompt, **kwargs):
            observed.append(prompt)
            action()
            return {'completed': True, 'final_response': 'Owner accepted the bounded handoff.', 'messages': []}

        def get_activity_summary(self):
            return {'seconds_since_activity': 0}

        def close(self):
            return None

    module = ModuleType('run_agent')
    module.AIAgent = Owner
    monkeypatch.setitem(sys.modules, 'run_agent', module)
    monkeypatch.setattr(runtime_provider, 'resolve_runtime_provider', lambda **kwargs: {
        'provider': 'custom', 'api_mode': 'chat_completions',
        'base_url': 'http://127.0.0.1:1/v1', 'api_key': 'fixture-only'})
    monkeypatch.setattr(scheduler, '_resolve_delivery_target', lambda job: None)
    monkeypatch.setattr(scheduler, '_resolve_cron_enabled_toolsets', lambda job, cfg: [])
    monkeypatch.setenv('HERMES_CRON_TIMEOUT', '0')
    return observed


def test_missed_completion_reaches_actual_worker_then_repeated_checks_stay_quiet(workflow, monkeypatch):
    home, conn, job, source, target, profile = workflow
    result = home / 'accepted-review.txt'
    processes = []

    def accept():
        # The recipient opens its own connection, as a fresh owner would.
        with kbc.connect_closing() as owner:
            wake = continuation.collect_wakeup(owner, job_id=job['id'], profile=profile)
            args = dict(card=source, event=wake['event'], profile=profile,
                        job_id=job['id'], next_card=target)
            assert continuation.bind_next_selection(owner, **args)

            def spawn(task, workspace):
                process = subprocess.Popen([sys.executable, '-c',
                    'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("review accepted")',
                    str(result)])
                processes.append(process)
                return process.pid

            dispatched = dispatch.dispatch_once(owner, spawn_fn=spawn, max_in_progress=1)
            assert [row[0] for row in dispatched.spawned] == [target]
            assert processes[-1].wait(timeout=10) == 0
            effect = next(event.id for event in reversed(kb.list_events(owner, target))
                          if event.kind == 'spawned')
            assert continuation.acknowledge_next_selection(owner, **args, effect_event=effect)

    observed = recording_owner(monkeypatch, accept)
    with SessionDB() as db:
        db.create_session('initiating-conversation', 'telegram')
        before = db.get_messages('initiating-conversation')
        # Deliberately omit notification; the existing script gate recovers the transition.
        kb.complete_task(conn, source, summary='Implementation result verified for review.')
        try:
            assert run_job(get_job(job['id']))[0]
            assert result.read_text() == 'review accepted'
            for _ in range(3):
                outcome = run_job(get_job(job['id']))
                assert outcome[2] == SILENT_MARKER, outcome[1]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
        assert db.get_messages('initiating-conversation') == before
    assert len(observed) == 1
    assert (home / 'gate-calls.txt').read_text() == '4'
    assert len(kb.list_runs(conn, target)) == 1


def test_interrupted_owner_remains_retryable_without_a_new_event(workflow, monkeypatch):
    _home, conn, job, source, _target, profile = workflow
    kb.complete_task(conn, source, summary='Bounded result awaits the owner.')

    def interrupted():
        raise RuntimeError('Recipient interrupted before accepting work')

    observed = recording_owner(monkeypatch, interrupted)
    assert not run_job(get_job(job['id']))[0]
    assert not run_job(get_job(job['id']))[0]
    assert len(observed) == 2
    assert continuation.collect_wakeup(conn, job_id=job['id'], profile=profile)['card'] == source
    pause_job(job['id'], reason='Current operator pause')
    outcome = run_job(get_job(job['id']))
    assert outcome[2] == SILENT_MARKER, outcome[1]
    assert len(observed) == 2


def test_failed_gate_reports_error_without_starting_owner(workflow, monkeypatch):
    home, _conn, job, _source, _target, _profile = workflow
    (home / 'scripts' / 'gate.py').write_text('raise RuntimeError("board unavailable")\n')
    observed = recording_owner(monkeypatch, lambda: pytest.fail('Owner must not start'))
    outcome = run_job(get_job(job['id']))
    assert not outcome[0]
    assert 'board unavailable' in outcome[3]
    assert observed == []
