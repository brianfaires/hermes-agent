"""Deterministic selected-context failures park once and preserve candidate work (#32)."""
from pathlib import Path
import hashlib
import subprocess
import sys
import uuid

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from tests.waiting import wait_until


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    profile = home / 'profiles' / 'reviewer'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('terminal: {backend: local}\n')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    workspace = tmp_path / 'candidate'
    workspace.mkdir()
    (workspace / 'deliverable.txt').write_text('preserved candidate')
    marker = workspace / 'accepted.txt'
    monkeypatch.setattr(dispatch, '_worker_argv', lambda *a: [sys.executable, '-c',
        f'from pathlib import Path; Path({str(marker)!r}).write_text("accepted")'])
    with kbc.connect_closing() as conn:
        yield conn, profile, workspace, marker


def test_missing_required_review_skill_parks_once_before_executor_launch(workflow):
    conn, profile, workspace, marker = workflow
    card = kb.create_task(conn, title='Review approved candidate', assignee='reviewer',
                          workspace_kind='dir', workspace_path=str(workspace))
    assert kb.request_review(conn, card, reviewer='reviewer', summary='Candidate ready.')
    assert dispatch.dispatch_once(conn).spawned == []
    task = kb.get_task(conn, card)
    assert task.status == 'blocked' and task.block_kind == 'capability'
    assert 'sdlc-review' in kb.list_runs(conn, card)[-1].summary
    events = kb.list_events(conn, card)
    for _ in range(2):
        assert dispatch.dispatch_once(conn).spawned == []
    assert kb.list_events(conn, card) == events
    assert not marker.exists()
    assert (workspace / 'deliverable.txt').read_text() == 'preserved candidate'
    skill = profile / 'skills' / 'sdlc-review' / 'SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: sdlc-review\ndescription: Review the bounded candidate.\n---\n'
                     'Read the deliverable and report its actual result.\n')
    assert kb.unblock_task(conn, card)
    assert [row[0] for row in dispatch.dispatch_once(conn).spawned] == [card]
    assert wait_until(marker.exists, timeout=10)
    assert marker.read_text() == 'accepted'
    assert kb.complete_task(conn, card, summary='Declared candidate reviewed.',
                            expected_run_id=kb.get_task(conn, card).current_run_id)


@pytest.mark.parametrize('workspace_kind', ['dir', 'scratch'])
def test_unusable_selected_workspace_route_parks_once_without_losing_candidate(workflow, workspace_kind):
    conn, profile, workspace, marker = workflow
    # This profile's configured route is unavailable, even though the host can execute.
    (profile / 'config.yaml').write_text('terminal: {backend: nonexistent-test-backend}\n')
    card = kb.create_task(conn, title='Execute approved candidate', assignee='reviewer',
                          workspace_kind=workspace_kind,
                          workspace_path=str(workspace) if workspace_kind == 'dir' else None)
    assert dispatch.dispatch_once(conn).spawned == []
    task = kb.get_task(conn, card)
    assert task.status == 'blocked' and task.block_kind == 'capability'
    assert 'nonexistent-test-backend' in kb.list_runs(conn, card)[-1].summary
    for _ in range(2):
        assert dispatch.dispatch_once(conn).spawned == []
    assert len(kb.list_runs(conn, card)) == 1
    assert not marker.exists()
    assert (workspace / 'deliverable.txt').read_text() == 'preserved candidate'
    (profile / 'config.yaml').write_text('terminal: {backend: local}\n')
    assert kb.unblock_task(conn, card)
    assert [row[0] for row in dispatch.dispatch_once(conn).spawned] == [card]
    assert wait_until(marker.exists, timeout=10)
    assert kb.complete_task(conn, card, summary='Actual selected route accepted candidate.',
                            expected_run_id=kb.get_task(conn, card).current_run_id)


def test_executor_type_error_after_effect_does_not_repeat_launch(workflow):
    conn, _profile, _workspace, marker = workflow
    card = kb.create_task(conn, title='One launch attempt', assignee='reviewer')

    def spawn(task, workspace):
        marker.write_text(marker.read_text() + 'launch\n' if marker.exists() else 'launch\n')
        raise TypeError('Executor failed after its launch effect')

    assert dispatch.dispatch_once(conn, spawn_fn=spawn).spawned == []
    assert marker.read_text() == 'launch\n'
    assert kb.get_task(conn, card).consecutive_failures == 1


@pytest.mark.parametrize('persistent', [False, True])
def test_probe_binds_selected_workspace_before_container_execution(workflow, monkeypatch, persistent):
    conn, profile, workspace, marker = workflow
    import tools.terminal_tool as terminal
    from tools import terminal_tool_backends

    (profile / 'config.yaml').write_text(
        f'terminal:\n  backend: docker\n  cwd: {profile}\n'
        f'  docker_mount_cwd_to_workspace: true\n  container_persistent: {str(persistent).lower()}\n')

    class MountedContainer:
        """External engine boundary: execute a real command only in the declared mount."""
        env_type = 'docker'

        def __init__(self, *, cwd, host_cwd, task_id, **kwargs):
            self.cwd, self.host_cwd = cwd, host_cwd
            identity = hashlib.sha256(task_id.encode()).hexdigest()[:16]
            self.marker = workspace / f'sandbox-{identity}.txt'
            self.marker.write_text(uuid.uuid4().hex)

        def execute(self, command, cwd='', **kwargs):
            if self.host_cwd not in {str(workspace), str(profile)} or cwd != '/workspace':
                return {'returncode': 126, 'output': 'Selected workspace is not mounted'}
            result = subprocess.run(command, shell=True, cwd=self.host_cwd,
                                    capture_output=True, text=True, timeout=10, check=False)
            (Path(self.host_cwd) / 'probe-output.txt').write_text(result.stdout)
            return {'returncode': result.returncode, 'output': result.stdout}

        def cleanup(self, **kwargs):
            self.marker.unlink()

    monkeypatch.setattr(terminal, 'check_terminal_requirements', lambda: True)
    monkeypatch.setattr(terminal_tool_backends, '_create_environment',
                        lambda **kwargs: MountedContainer(**kwargs))
    from tools.terminal_tool_lifecycle import cleanup_vm
    with dispatch._worker_profile_scope(str(profile)):
        terminal.register_task_env_overrides('existing-owner', {'cwd': str(profile), 'cwd_source': 'session'})
        import json
        initial = json.loads(terminal.terminal_tool('echo existing-owner', task_id='existing-owner', timeout=15))
        assert initial['exit_code'] == 0
        owner_key = terminal._resolve_container_task_id('existing-owner')
    owner_sandboxes = {path.name: path.read_text() for path in workspace.glob('sandbox-*.txt')}
    card = kb.create_task(conn, title='Execute in the assigned mounted workspace', assignee='reviewer',
                          workspace_kind='dir', workspace_path=str(workspace))
    assert [row[0] for row in dispatch.dispatch_once(conn).spawned] == [card]
    assert wait_until(marker.exists, timeout=10)
    assert marker.read_text() == 'accepted'
    assert (workspace / 'probe-output.txt').read_text().strip() == 'hermes-launch-probe'
    remaining = {path.name: path.read_text() for path in workspace.glob('sandbox-*.txt')}
    assert remaining == owner_sandboxes, 'Probe must remove its sandbox and preserve the live owner environment'
    assert kb.complete_task(conn, card, summary='Selected mounted workspace accepted.',
                            expected_run_id=kb.get_task(conn, card).current_run_id)
    with dispatch._worker_profile_scope(str(profile)):
        cleanup_vm(owner_key, force_remove=True)
        terminal.clear_task_env_overrides('existing-owner')
