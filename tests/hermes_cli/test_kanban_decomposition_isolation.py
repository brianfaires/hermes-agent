"""Real graph prerequisites and exclusive directory claims across a board fleet."""
import hermes_bootstrap  # Settle worktree bootstrap before disposable-state guards.

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
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


def test_review_claims_wait_for_overlaps_but_allow_independent_directories(home, tmp_path):
    with kbc.connect_closing() as conn:
        writer = kb.create_task(conn, title='Writer', workspace_kind='dir',
                                workspace_path=str(tmp_path / 'project'))
        review = kb.create_task(conn, title='Review', workspace_kind='dir',
                                workspace_path=str(tmp_path / 'project' / 'nested'))
        independent = kb.create_task(conn, title='Independent', workspace_kind='dir',
                                     workspace_path=str(tmp_path / 'other'))
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (review,))
        assert kb.claim_task(conn, writer)
        assert kb.claim_review_task(conn, review) is None
        assert kb.claim_task(conn, independent)
        assert _complete(conn, writer)
        assert kb.claim_review_task(conn, review)


def test_explicit_scratch_paths_obey_the_same_overlap_policy(home, tmp_path):
    shared = str(tmp_path / 'shared')
    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title='Fan-out', triage=True)
        with pytest.raises(ValueError, match='overlap'):
            decompose_triage_task(conn, root, root_assignee=None, children=[
                {'title': 'First', 'workspace_kind': 'scratch', 'workspace_path': shared},
                {'title': 'Second', 'workspace_kind': 'scratch', 'workspace_path': shared},
            ])
        scratch = kb.create_task(conn, title='Scratch writer', workspace_kind='scratch', workspace_path=shared)
        directory = kb.create_task(conn, title='Directory writer', workspace_kind='dir', workspace_path=shared)
        assert kb.claim_task(conn, scratch)
        assert kb.claim_task(conn, directory) is None
        assert _complete(conn, scratch)
        assert kb.claim_task(conn, directory)


def test_worktree_destinations_are_exclusive_but_repository_anchors_are_independent(home, tmp_path):
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
    (repo / 'README').write_text('Fixture repository')
    subprocess.run(['git', '-C', str(repo), 'add', 'README'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                    'commit', '-m', 'Initialize'], check=True, capture_output=True)
    with kbc.connect_closing() as conn:
        first = kb.create_task(conn, title='First', workspace_kind='worktree', workspace_path=str(repo))
        second = kb.create_task(conn, title='Second', workspace_kind='worktree', workspace_path=str(repo))
        assert kb.claim_task(conn, first)
        assert kb.claim_task(conn, second)
        destination = workspaces.resolve_workspace(kb.get_task(conn, first))
        workspaces.set_workspace_path(conn, first, destination)
        same = kb.create_task(conn, title='Same checkout', workspace_kind='worktree',
                             workspace_path=str(destination), branch_name=f'wt/{first}')
        direct = kb.create_task(conn, title='Direct writer', workspace_kind='dir', workspace_path=str(destination))
        assert kb.claim_task(conn, same) is None
        assert kb.claim_task(conn, direct) is None
        assert kb.get_task(conn, same).current_run_id is None


def test_implicit_destinations_are_exclusive_before_materialization(home, tmp_path):
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
    (repo / 'README').write_text('Fixture repository')
    subprocess.run(['git', '-C', str(repo), 'add', 'README'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                    'commit', '-m', 'Initialize'], check=True, capture_output=True)
    kb.create_board('other', default_workdir=str(repo))
    with kbc.connect_closing(kb.kanban_db_path('other')) as conn:
        root = kb.create_task(conn, title='Root', triage=True, workspace_kind='worktree')
        child, = decompose_triage_task(conn, root, root_assignee=None, children=[{'title': 'Child'}])
        direct = kb.create_task(conn, title='Direct', workspace_kind='dir',
                                workspace_path=str(repo / '.worktrees' / child))
        assert kb.claim_task(conn, direct)
        assert kb.claim_task(conn, child) is None
        assert not (repo / '.worktrees' / child).exists()
        assert _complete(conn, direct)
        assert kb.claim_task(conn, child)
        later_direct = kb.create_task(conn, title='Later direct', workspace_kind='dir',
                                      workspace_path=str(repo / '.worktrees' / child))
        assert kb.claim_task(conn, later_direct) is None
        scratch = kb.create_task(conn, title='Scratch')
        scratch_direct = kb.create_task(conn, title='Scratch direct', workspace_kind='dir',
                                        workspace_path=str(kb.workspaces_root(board='other') / scratch))
        assert kb.claim_task(conn, scratch)
        assert kb.claim_task(conn, scratch_direct) is None


@pytest.mark.parametrize('review', [False, True])
def test_invalid_workspace_only_blocks_its_card_and_other_work_starts(home, review):
    from hermes_cli import kanban_db_dispatch as dispatch
    launched = []
    def spawn(task, workspace, *, board=None):
        launched.append(task.id)
    with kbc.connect_closing() as conn:
        bad = kb.create_task(conn, title='Missing worktree repository', assignee='default',
                             workspace_kind='worktree', priority=100)
        if review:
            assert kb.request_review(conn, bad, summary='Candidate ready for independent review.')
        good = kb.create_task(conn, title='Independent ordinary work', assignee='default')
        result = dispatch.dispatch_once(conn, spawn_fn=spawn)
        assert good in launched and bad not in launched
        blocked = kb.get_task(conn, bad)
        assert blocked.status == 'blocked', [(e.kind, e.payload) for e in kb.list_events(conn, bad)]
        assert bad in result.auto_blocked
        assert 'default_workdir' in blocked.last_failure_error
        assert any(e.kind == 'gave_up' and 'default_workdir' in str(e.payload)
                   for e in kb.list_events(conn, bad))
        assert not conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND status='running'", (bad,)).fetchone()


def test_unplannable_running_workspace_defers_claim_without_aborting_tick(home):
    from hermes_cli import kanban_db_dispatch as dispatch
    with kbc.connect_closing() as conn:
        running = kb.create_task(conn, title='Legacy worker', assignee='default')
        assert kb.claim_task(conn, running)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind='worktree', workspace_path=NULL WHERE id=?", (running,))
        waiting = kb.create_task(conn, title='Next task', assignee='default')
        result = dispatch.dispatch_once(conn, spawn_fn=lambda *a: None)
        assert result.spawned == []
        assert kb.get_task(conn, waiting).status == 'ready'
        events = kb.list_events(conn, waiting)
        assert any(e.kind == 'claim_rejected' and running in str(e.payload) for e in events)


def test_stale_workspace_error_does_not_block_a_repaired_new_attempt(home, monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli.kanban_workspace_claims import WorkspacePlanError
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title='Repaired while old attempt failed', assignee='default',
                              workspace_kind='worktree')
        native_claim = kb.claim_task
        taken = []
        def repaired_claim(connection, task_id, **kwargs):
            with kb.write_txn(connection):
                connection.execute("UPDATE tasks SET workspace_kind='scratch' WHERE id=?", (task_id,))
            taken.append(native_claim(connection, task_id, **kwargs))
            raise WorkspacePlanError('outdated workspace failure')
        monkeypatch.setattr(kb, 'claim_task', repaired_claim)
        result = dispatch.dispatch_once(conn, spawn_fn=lambda *a: None)
        current = kb.get_task(conn, task)
        assert taken[0] and current.current_run_id == taken[0].current_run_id
        assert current.status == 'running' and current.consecutive_failures == 0
        assert task not in result.auto_blocked
