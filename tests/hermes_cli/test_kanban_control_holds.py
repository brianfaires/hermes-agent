"""Operator holds survive triage, legacy creation and stale status writers."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose, kanban_specify
from hermes_cli.kanban_db_graph import decompose_triage_task


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def _loop_hold(conn):
    task = kb.create_task(conn, title='Needs operator decision')
    assert kb.block_task(conn, task, kind='needs_input')
    assert kb.unblock_task(conn, task)
    assert kb.block_task(conn, task, kind='needs_input')
    assert kb.get_task(conn, task).status == 'triage'
    return task


def test_legacy_creation_hold_stays_until_explicit_release(board):
    task = kb.create_task(board, title='Legacy parked card', initial_status='blocked')
    board.execute("DELETE FROM task_events WHERE task_id=? AND kind='blocked'", (task,))
    board.commit()
    assert kb.recompute_ready(board) == 0
    assert kb.get_task(board, task).status == 'blocked'
    assert kb.unblock_task(board, task)
    assert kb.claim_task(board, task) is not None


def test_triage_hold_refuses_specification_and_decomposition(board, monkeypatch):
    task = _loop_hold(board)
    calls = []
    monkeypatch.setattr(kanban_decompose, '_call_aux', lambda *a, **kw: calls.append(kw))
    loaded, reason = kanban_specify._load_triage_task(task)
    assert loaded is None and 'held' in reason
    assert not kanban_decompose.decompose_task(task).ok
    assert not calls
    assert task not in kanban_decompose.list_triage_ids()
    assert task not in kanban_specify.list_triage_ids()
    assert not kb.specify_triage_task(board, task, title='Accidental release')
    assert decompose_triage_task(board, task, root_assignee=None,
                                 children=[{'title': 'Must not spawn'}]) is None
    assert len(kb.list_tasks(board)) == 1
    assert kb.get_task(board, task).status == 'triage'


@pytest.mark.parametrize('stale_status', ['todo', 'ready', 'review'])
def test_stale_status_cannot_release_control_hold(board, stale_status):
    task = _loop_hold(board)
    board.execute('UPDATE tasks SET status=? WHERE id=?', (stale_status, task))
    board.commit()
    assert kb.recompute_ready(board) == 0
    assert kb.claim_task(board, task) is None
    assert kb.claim_review_task(board, task) is None
    assert kb.get_task(board, task).current_run_id is None


def test_explicit_unblock_releases_triage_hold(board):
    task = _loop_hold(board)
    assert kb.unblock_task(board, task)
    assert kb.get_task(board, task).status == 'ready'
    assert kb.claim_task(board, task) is not None


def test_explicit_manual_promotion_releases_legacy_hold(board):
    task = kb.create_task(board, title='Parked', initial_status='blocked')
    assert kb.promote_task(board, task, actor='operator')[0]
    assert kb.claim_task(board, task) is not None


def test_triage_release_preserves_review_phase(board):
    task = kb.create_task(board, title='Review requires operator decision')
    assert kb.request_review(board, task, reviewer='reviewer')
    assert kb.claim_review_task(board, task)
    assert kb.block_task(board, task, kind='needs_input')
    assert kb.unblock_task(board, task)
    assert kb.get_task(board, task).status == 'review'
    assert kb.claim_review_task(board, task)
    assert kb.block_task(board, task, kind='needs_input')
    assert kb.get_task(board, task).status == 'triage'
    assert kb.unblock_task(board, task)
    assert kb.get_task(board, task).status == 'review'
