"""Creation and decomposition honor supported workspace and branch metadata."""
from pathlib import Path
import subprocess

import pytest

import run_agent
from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_workspace as workspace
from hermes_cli.kanban_db_graph import decompose_triage_task


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


@pytest.mark.parametrize('branch', ['-option', 'bad..ref', 'topic/.hidden', 'topic.lock', 'HEAD', 'bad\nref'])
def test_invalid_git_branch_is_rejected_before_card_or_fanout(board, branch):
    with pytest.raises(ValueError):
        kb.create_task(board, title='Invalid', workspace_kind='worktree', branch_name=branch)
    root = kb.create_task(board, title='Root', triage=True, workspace_kind='worktree')
    with pytest.raises(ValueError):
        decompose_triage_task(board, root, root_assignee=None,
                              children=[{'title': 'Invalid child', 'branch_name': branch}])
    assert len(kb.list_tasks(board)) == 1
    assert kb.get_task(board, root).status == 'triage'


@pytest.mark.parametrize('metadata', [
    {'workspace_kind': 'unknown'},
    {'workspace_kind': 'dir', 'workspace_path': 'relative/project'},
    {'workspace_kind': 'scratch', 'branch_name': 'topic/branch'},
])
def test_invalid_workspace_metadata_is_atomic_on_both_entrypoints(board, metadata):
    with pytest.raises(ValueError):
        kb.create_task(board, title='Invalid', **metadata)
    root = kb.create_task(board, title='Root', triage=True)
    with pytest.raises(ValueError):
        decompose_triage_task(board, root, root_assignee=None,
                              children=[{'title': 'Invalid child', **metadata}])
    assert len(kb.list_tasks(board)) == 1


def test_decomposed_child_materializes_requested_supported_git_branch(board, tmp_path):
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
    (repo / 'README').write_text('Fixture repository')
    subprocess.run(['git', '-C', str(repo), 'add', 'README'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                    'commit', '-m', 'Initialize'], check=True, capture_output=True)
    root = kb.create_task(board, title='Root', triage=True)
    requested = 'feature/real.v1'
    target = repo / '.worktrees' / 'child'
    child, = decompose_triage_task(board, root, root_assignee=None, children=[{
        'title': 'Implement', 'workspace_kind': 'worktree', 'workspace_path': str(target),
        'branch_name': requested, 'assignee': 'Default'}])
    task = kb.get_task(board, child)
    assert (task.branch_name, task.assignee) == (requested, 'default')
    assert workspace.resolve_workspace(task) == target
    branch = subprocess.run(['git', '-C', str(target), 'branch', '--show-current'],
                            check=True, capture_output=True, text=True).stdout.strip()
    assert branch == requested


def test_board_default_directory_and_empty_optional_branch_remain_supported(board, tmp_path):
    project = tmp_path / 'project'
    kb.create_board('default', default_workdir=str(project))
    task = kb.create_task(board, title='Board default', workspace_kind='dir')
    assert workspace.resolve_workspace(kb.get_task(board, task)) == project
    scratch = kb.create_task(board, title='Scratch', branch_name='')
    assert kb.get_task(board, scratch).branch_name is None
    assert cli._parse_branch_flag(' feature/real.v1 ') == 'feature/real.v1'


def test_missing_assignee_stays_unassigned_without_configured_fallback(board):
    task = kb.create_task(board, title='Needs assignee')
    calls = []
    result = dispatch.dispatch_once(board, spawn_fn=lambda *args, **kw: calls.append(args), default_assignee=None)
    assert task in result.skipped_unassigned
    assert not calls and kb.get_task(board, task).assignee is None
    with pytest.raises(ValueError):
        kb.create_task(board, title='Numeric identity', assignee=0)
