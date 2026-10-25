"""Workspace failures remain visible and cannot bypass or starve native claims."""
import errno
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli.test_kanban_decomposition_isolation import home


@pytest.mark.parametrize('board', ['default', 'other'])
def test_unknown_running_directory_defers_same_and_sibling_board_claims(home, tmp_path, board):
    kb.create_board('other')
    with kbc.connect_closing(kb.kanban_db_path(board)) as writer:
        task = kb.create_task(writer, title='Existing writer', workspace_kind='dir',
                              workspace_path=str(tmp_path / 'project'))
        assert kb.claim_task(writer, task)
        with kb.write_txn(writer):
            writer.execute('UPDATE tasks SET workspace_path=NULL WHERE id=?', (task,))
    with kbc.connect_closing(kb.kanban_db_path('default')) as conn:
        waiting = kb.create_task(conn, title='Waiting for a known workspace')
        assert kb.claim_task(conn, waiting) is None
        assert kb.get_task(conn, waiting).status == 'ready'
        assert kb.get_task(conn, waiting).current_run_id is None
        assert any(e.kind == 'claim_rejected' and task in str(e.payload)
                   for e in kb.list_events(conn, waiting))


@pytest.mark.parametrize('failure', [None, errno.ENOLCK])
def test_workspace_lock_errors_are_visible_but_contention_stays_quiet(home, monkeypatch, caplog, failure):
    def try_lock(handle):
        if failure is not None:
            raise OSError(failure, 'fixture filesystem has no locks')
        return False
    monkeypatch.setattr(kbc, '_try_lock_nb', try_lock)
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title='Await filesystem lock')
        caplog.clear()
        assert kb.claim_task(conn, task) is None
        assert kb.get_task(conn, task).status == 'ready'
        assert kb.get_task(conn, task).current_run_id is None
        if failure is not None:
            assert 'fixture filesystem has no locks' in caplog.text
            assert str(home / 'kanban' / '.workspace-claims.lock') in caplog.text
            assert 'filesystem' in caplog.text
        else:
            assert not caplog.records


@pytest.mark.platforms('linux', 'macos')
@pytest.mark.parametrize('legacy_error', [False, True])
def test_symlink_loop_pauses_only_invalid_card(home, tmp_path, monkeypatch, legacy_error):
    from hermes_cli import kanban_db_dispatch as dispatch
    loop = tmp_path / 'loop'
    loop.symlink_to(loop)
    if legacy_error:
        # Python 3.11/3.12 report this filesystem failure as RuntimeError.
        resolve = Path.resolve
        def resolve_path(path, *args, **kwargs):
            if path == loop:
                raise RuntimeError('Symlink loop from fixture filesystem')
            return resolve(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'resolve', resolve_path)
    launched = []
    def spawn(task, workspace, *, board=None):
        launched.append(task.id)
    with kbc.connect_closing() as conn:
        invalid = kb.create_task(conn, title='Invalid directory', assignee='default', priority=100)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?", (str(loop), invalid))
        good = kb.create_task(conn, title='Valid independent task', assignee='default')
        result = dispatch.dispatch_once(conn, spawn_fn=spawn)
        assert good in launched and invalid not in launched
        assert kb.get_task(conn, invalid).last_failure_error
        if legacy_error:
            assert invalid in result.auto_blocked
            assert kb.get_task(conn, invalid).status == 'blocked'
