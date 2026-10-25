"""Explicit IDs do not grant cron or delegated work its parent's lifecycle."""
from contextlib import contextmanager
import json
import os
from pathlib import Path

import pytest

import run_agent
from agent.delegation_context import delegated_child_context, delegated_child_subprocess_env
from cron.scheduler import _CronRunScope
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as workspaces
from tools import kanban_tools as tools


@pytest.fixture
def parent(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    monkeypatch.delenv('HERMES_DELEGATED_CHILD_CONTEXT', raising=False)
    kb.init_db()
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title='Parent deliverable', assignee='owner')
        claimed = kb.claim_task(conn, task)
        workspace = workspaces.resolve_workspace(claimed)
        workspaces.set_workspace_path(conn, task, workspace)
        artifact = workspace / 'parent-evidence.txt'
        artifact.write_text('parent workspace must survive child attempts')
        monkeypatch.setenv('HERMES_KANBAN_TASK', task)
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(claimed.current_run_id))
        monkeypatch.setenv('HERMES_KANBAN_DB', str(kb.kanban_db_path()))
        yield conn, task, claimed.current_run_id, artifact


@contextmanager
def _execution(kind):
    if kind == 'delegate':
        with delegated_child_context('fixture-child'):
            yield
    else:
        scope = _CronRunScope({'id': 'fixture', 'workdir': ''}, 'fixture', 'execution')
        scope.enter()
        try:
            yield
        finally:
            scope.exit()


@pytest.mark.parametrize('kind', ['cron', 'delegate'])
def test_explicit_parent_id_cannot_mutate_or_create_overlapping_writer(parent, kind):
    conn, task, run_id, artifact = parent
    before = kb.list_events(conn, task)
    with _execution(kind):
        assert json.loads(tools._handle_show({'task_id': task}))['task']['id'] == task
        complete = json.loads(tools._handle_complete({'task_id': task, 'summary': 'Unauthorized parent completion.'}))
        block = json.loads(tools._handle_block({'task_id': task, 'reason': 'Unauthorized parent hold.'}))
        create = json.loads(tools._handle_create({'title': 'Overlapping writer', 'assignee': 'owner',
                                                 'workspace_kind': 'dir', 'workspace_path': str(artifact.parent)}))
        assert all('error' in result for result in (complete, block, create))
        with pytest.raises(PermissionError):
            kb.complete_task(conn, task, expected_run_id=run_id, summary='Even explicit run identity is inherited.')
    assert artifact.read_text() == 'parent workspace must survive child attempts'
    assert kb.get_task(conn, task).status == 'running'
    assert kb.list_events(conn, task) == before
    assert len(kb.list_tasks(conn)) == 1
    assert kb.complete_task(conn, task, expected_run_id=run_id, summary='Parent owner completed its actual deliverable.')
    assert not artifact.exists()


def test_cron_lineage_fence_survives_scope_changes_but_allows_unrelated_scratch(parent, tmp_path, monkeypatch):
    conn, task, run_id, _artifact = parent
    with _execution('cron'):
        with monkeypatch.context() as patch:
            patch.delenv('HERMES_KANBAN_TASK')
            scratch = tmp_path / 'scratch'
            scratch.mkdir()
            patch.setenv('HERMES_HOME', str(scratch))
            patch.setenv('HERMES_KANBAN_HOME', str(scratch))
            with kbc.connect_closing(scratch / 'kanban.db') as independent:
                assert kb.create_task(independent, title='Authorized scratch repro')
            with pytest.raises(PermissionError):
                kb.block_task(conn, task, expected_run_id=run_id, reason='Must remain fenced')
            child_env = delegated_child_subprocess_env(dict(os.environ))
            assert child_env['HERMES_DELEGATED_CHILD_CONTEXT'] == str(tmp_path / '.hermes')
    assert kb.get_task(conn, task).status == 'running'


def test_standalone_cron_retains_authorized_kanban_work(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'independent'))
    for key in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_DB', 'HERMES_KANBAN_HOME', 'HERMES_DELEGATED_CHILD_CONTEXT'):
        monkeypatch.delenv(key, raising=False)
    with _execution('cron'):
        with kbc.connect_closing() as conn:
            assert kb.create_task(conn, title='Independent authorized cron work')
