"""Deterministic selected-context failures park once and preserve candidate work (#32)."""
from pathlib import Path
import sys

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


def test_unusable_selected_workspace_route_parks_once_without_losing_candidate(workflow):
    conn, profile, workspace, marker = workflow
    # This profile's configured route is unavailable, even though the host can execute.
    (profile / 'config.yaml').write_text('terminal: {backend: nonexistent-test-backend}\n')
    card = kb.create_task(conn, title='Execute approved candidate', assignee='reviewer',
                          workspace_kind='dir', workspace_path=str(workspace))
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
