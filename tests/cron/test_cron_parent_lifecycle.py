"""Worker-fired cron cannot claim its parent's lifecycle, even explicitly."""
import json
import os
from pathlib import Path
import pytest

from tests.cron.test_cron_kanban_env_isolation import TestRunJobKanbanIsolation as CronHarness


def test_real_cron_completion_preserves_running_parent(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    import cron.scheduler as sched

    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('toolsets: [kanban]\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path / 'board-home'))
    monkeypatch.setenv('HERMES_PROFILE', 'test-worker')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    conn = kb.connect()
    tid = kb.create_task(conn, title='parent evidence', assignee='test-worker')
    kb.claim_task(conn, tid)
    run = kb.latest_run(conn, tid)
    workspace = kb.resolve_workspace(kb.get_task(conn, tid))
    kb.set_workspace_path(conn, tid, workspace)
    sentinel = workspace / 'rollback.txt'
    sentinel.write_text('irreplaceable parent evidence')
    monkeypatch.setenv('HERMES_KANBAN_TASK', tid)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run.id))
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACE', str(workspace))
    observed = {}

    class AttemptingCron:
        def __init__(self, **kwargs):
            pass

        def run_conversation(self, *args, **kwargs):
            observed['completion'] = json.loads(kt._handle_complete({
                'task_id': tid, 'summary': 'child must not sign off parent',
            }))
            return {'final_response': 'cron finished', 'messages': []}

        def get_activity_summary(self):
            return {'seconds_since_activity': 0.0}

    CronHarness._install_stubs(monkeypatch, observed, AttemptingCron)
    try:
        success, *_ = sched.run_job(CronHarness._job())
        assert success
        assert sentinel.exists(), 'child completion deleted active parent workspace'
        assert sentinel.read_text() == 'irreplaceable parent evidence'
        assert kb.get_task(conn, tid).status == 'running'
        assert kb.latest_run(conn, tid).id == run.id
        assert kb.latest_run(conn, tid).outcome is None
        assert 'error' in observed['completion']
        # The owning worker can still finish, exercising real cleanup positively.
        result = json.loads(kt._handle_complete({'task_id': tid, 'summary': 'owner done'}))
        assert result['ok']
        assert not workspace.exists()
    finally:
        conn.close()


def test_script_only_cron_does_not_export_worker_identity(monkeypatch, tmp_path):
    import cron.scheduler as sched

    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_parent')
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', '42')
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACE', str(tmp_path / 'parent'))
    script = Path(os.environ['HERMES_HOME']) / 'scripts' / 'probe.py'
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        'import os,json\nfrom pathlib import Path\n'
        'Path("cron-write.txt").write_text("child still works")\n'
        'print(json.dumps({k:v for k,v in os.environ.items() '
        'if k.startswith("HERMES_KANBAN_") or k == "HERMES_DELEGATED_CHILD_CONTEXT"}))\n'
    )
    success, _, output, error = sched.run_job({
        'id': 'isolated-script', 'name': 'isolated-script', 'no_agent': True,
        'script': str(script), 'workdir': str(tmp_path),
    })
    assert success, error
    values = json.loads(output)
    assert not any(k.startswith('HERMES_KANBAN_') for k in values), values
    assert values['HERMES_DELEGATED_CHILD_CONTEXT'] == '1'
    assert (tmp_path / 'cron-write.txt').read_text() == 'child still works'
    assert os.environ['HERMES_KANBAN_TASK'] == 't_parent'


def test_cron_child_context_reaches_subprocess_and_all_mutation_guards(monkeypatch, tmp_path):
    import subprocess
    import sys
    from agent.delegation_context import non_dispatcher_owned_context
    from tools.environments.local import hermes_subprocess_env

    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_parent')
    repo = str(Path(__file__).resolve().parents[2])
    with non_dispatcher_owned_context():
        env = hermes_subprocess_env()
    # Deliberately restore leaked identity: the durable child marker must still deny.
    env['HERMES_KANBAN_TASK'] = 't_parent'
    code = (
        f'import sys; sys.path.insert(0, {repo!r}); '
        'from tools import kanban_tools as kt; '
        'from hermes_cli import kanban_db as kb; '
        'import json; '
        'print(kt._handle_complete({"task_id":"t_parent","summary":"forbidden"})); '
        'kb._assert_not_delegated_child_mutation()'
    )
    result = subprocess.run([sys.executable, '-c', code], env=env,
                            capture_output=True, text=True, cwd=tmp_path, timeout=30)
    assert result.returncode != 0
    assert 'error' in json.loads(result.stdout.strip())
    assert 'PermissionError' in result.stderr


@pytest.mark.parametrize('name,args', [
    ('_handle_complete', {'task_id': 't_parent', 'summary': 'no'}),
    ('_handle_block', {'task_id': 't_parent', 'reason': 'no'}),
    ('_handle_heartbeat', {'task_id': 't_parent'}),
    ('_handle_comment', {'task_id': 't_parent', 'body': 'no'}),
    ('_handle_request_review', {'task_id': 't_parent', 'summary': 'no'}),
])
def test_worker_cron_handlers_deny_before_db(monkeypatch, name, args):
    from agent.delegation_context import non_dispatcher_owned_context
    from tools import kanban_tools as kt

    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_parent')
    monkeypatch.setattr(kt, '_connect', lambda **kw: pytest.fail('DB opened by child'))
    with non_dispatcher_owned_context():
        assert 'error' in json.loads(getattr(kt, name)(args))


def test_standalone_cron_keeps_configured_orchestration(monkeypatch):
    from agent.delegation_context import non_dispatcher_owned_context
    from tools import kanban_tools as kt

    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setattr(kt, '_profile_has_kanban_toolset', lambda: True)
    with non_dispatcher_owned_context():
        assert kt._check_kanban_orchestrator_mode()
        assert kt._reject_delegated_child_mutation('kanban_create') is None


def test_worker_cron_cannot_reuse_profile_tool_cache_or_session_mirror(monkeypatch):
    from agent.delegation_context import non_dispatcher_owned_context
    from gateway.session_context import set_current_session_id, get_session_env
    from tools import kanban_tools as kt
    from model_tools import get_tool_definitions

    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_parent')
    monkeypatch.setenv('HERMES_SESSION_ID', 'parent-session')
    monkeypatch.setattr(kt, '_profile_has_kanban_toolset', lambda: True)
    def names():
        return {d['function']['name'] for d in get_tool_definitions(
            enabled_toolsets=['kanban', 'terminal'], quiet_mode=True)}
    assert 'kanban_complete' in names()
    with non_dispatcher_owned_context():
        set_current_session_id('cron-child')
        assert get_session_env('HERMES_SESSION_ID') == 'cron-child'
        assert os.environ['HERMES_SESSION_ID'] == 'parent-session'
        assert 'terminal' in names()
        assert not any(n.startswith('kanban_') for n in names())
    assert 'kanban_complete' in names()
