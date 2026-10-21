"""Real graph prerequisites and exclusive directory claims across a board fleet."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as workspaces
from hermes_cli.kanban_db_graph import decompose_triage_task


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / '.hermes'
    root.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(root))
    kb.init_db()
    return root


def _complete(conn, task):
    return kb.complete_task(conn, task,
                            summary='Fixture deliverable verified and available to successor.',
                            expected_run_id=kb.get_task(conn, task).current_run_id)


def test_nested_children_keep_external_prerequisite_and_retry_graph(home):
    with kbc.connect_closing() as conn:
        prerequisite = kb.create_task(conn, title='Deliver input')
        root = kb.create_task(conn, title='Root', triage=True, parents=[prerequisite])
        children = decompose_triage_task(conn, root, root_assignee='owner', children=[
            {'title': 'First'}, {'title': 'Second', 'parents': [0]}])
        assert children
        assert all(kb.get_task(conn, task).status == 'todo' for task in children)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (children[0],))
        nested = decompose_triage_task(conn, children[0], root_assignee='owner',
                                      children=[{'title': 'Nested'}])
        assert nested and kb.get_task(conn, nested[0]).status == 'todo'
        assert decompose_triage_task(conn, root, root_assignee=None,
                                     children=[{'title': 'Duplicate'}]) is None
        assert _complete(conn, prerequisite)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, nested[0]).status == 'ready'
        assert kb.get_task(conn, children[1]).status == 'todo'
        assert _complete(conn, nested[0])
        kb.recompute_ready(conn)
        assert _complete(conn, children[0])
        kb.recompute_ready(conn)
        assert kb.get_task(conn, children[1]).status == 'ready'


@pytest.mark.parametrize('overlap', ['same', 'nested', 'alias'])
def test_parallel_directory_overlap_is_atomic_refusal(home, tmp_path, overlap):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    other = workspace
    if overlap == 'nested':
        other = workspace / 'subdirectory'
    elif overlap == 'alias':
        other = tmp_path / 'alias'
        try:
            other.symlink_to(workspace, target_is_directory=True)
        except OSError:
            pytest.skip('host cannot create directory symlink')
    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title='Root', triage=True, workspace_kind='dir',
                              workspace_path=str(workspace))
        with pytest.raises(ValueError, match='overlap'):
            decompose_triage_task(conn, root, root_assignee=None, children=[
                {'title': 'First', 'workspace_path': str(workspace)},
                {'title': 'Second', 'workspace_path': str(other)}])
        assert len(kb.list_tasks(conn)) == 1
        assert kb.get_task(conn, root).status == 'triage'


def test_ordered_directory_reuse_stays_supported(home, tmp_path):
    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title='Sequential', triage=True, workspace_kind='dir',
                              workspace_path=str(tmp_path / 'project'))
        first, second = decompose_triage_task(conn, root, root_assignee=None, children=[
            {'title': 'First'}, {'title': 'Second', 'parents': [0]}])
        assert kb.claim_task(conn, first)
        assert kb.claim_task(conn, second) is None
        assert _complete(conn, first)
        kb.recompute_ready(conn)
        assert kb.claim_task(conn, second)


def test_materialized_scratch_root_is_not_inherited_by_children(home):
    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title='Scratch root', triage=True)
        root_path = workspaces.resolve_workspace(kb.get_task(conn, root))
        workspaces.set_workspace_path(conn, root, root_path)
        first, second = decompose_triage_task(conn, root, root_assignee=None,
                                            children=[{'title': 'First'}, {'title': 'Second'}])
        paths = [workspaces.resolve_workspace(kb.get_task(conn, task)) for task in (first, second)]
        assert len({root_path, *paths}) == 3
        assert all(path.is_dir() for path in paths)


def test_simultaneous_cross_board_claims_exclude_overlapping_directories(home, tmp_path):
    kb.create_board('other')
    paths = [kb.kanban_db_path('default'), kb.kanban_db_path('other')]
    tasks = []
    for index, db in enumerate(paths):
        with kbc.connect_closing(db) as conn:
            tasks.append(kb.create_task(conn, title='Writer', assignee='worker', workspace_kind='dir',
                                        workspace_path=str(tmp_path / 'shared' / ('sub' if index else ''))))
    barrier = Barrier(2)

    def claim(index):
        with kbc.connect_closing(paths[index]) as conn:
            barrier.wait(timeout=10)
            return kb.claim_task(conn, tasks[index]) is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sum(results) == 1
    winner = results.index(True)
    loser = 1 - winner
    with kbc.connect_closing(paths[loser]) as conn:
        assert kb.claim_task(conn, tasks[loser]) is None
    with kbc.connect_closing(paths[winner]) as conn:
        assert _complete(conn, tasks[winner])
    with kbc.connect_closing(paths[loser]) as conn:
        assert kb.claim_task(conn, tasks[loser])
