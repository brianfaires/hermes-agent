"""Capacity parking through dispatch preserves real prerequisites and operator holds (#30)."""
from pathlib import Path
import subprocess
import sys

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


def test_parked_run_releases_capacity_without_releasing_dependency_or_manual_hold(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    profile = home / 'profiles' / 'specialist'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('{}\n')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    processes = []

    def spawn(task, workspace):
        process = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                                   stdin=subprocess.PIPE)
        processes.append(process)
        return process.pid

    def tick(conn):
        return dispatch.dispatch_once(conn, spawn_fn=spawn, max_in_progress=1,
                                      max_in_progress_per_profile=1)

    def exit_worker():
        processes[-1].communicate(timeout=10)
        assert processes[-1].returncode == 0

    try:
        with kbc.connect_closing() as conn:
            feature = kb.create_task(conn, title='Approved bounded feature', assignee='specialist')
            first = tick(conn)
            assert [row[0] for row in first.spawned] == [feature]
            old_run = kb.get_task(conn, feature).current_run_id
            prerequisite = kb.create_task(conn, title='Genuine external prerequisite')
            kb.link_tasks(conn, prerequisite, feature, expected_child_run_id=old_run)
            assert kb.block_task(conn, feature, kind='dependency', reason='Needs actual prerequisite',
                                 expected_run_id=old_run)
            exit_worker()
            held = kb.create_task(conn, title='Explicitly held work', assignee='specialist')
            assert kb.block_task(conn, held, kind='needs_input', reason='Operator hold remains')
            unrelated = kb.create_task(conn, title='Unrelated eligible work', assignee='specialist')
            second = tick(conn)
            assert [row[0] for row in second.spawned] == [unrelated]
            assert kb.get_task(conn, feature).status == 'todo'
            assert kb.get_task(conn, held).status == 'blocked'
            assert not kb.complete_task(conn, feature, summary='Stale worker result', expected_run_id=old_run)
            assert tick(conn).spawned == []
            assert kb.complete_task(conn, unrelated, summary='Unrelated work finished.',
                                    expected_run_id=kb.get_task(conn, unrelated).current_run_id)
            exit_worker()
            assert tick(conn).spawned == []
            assert kb.complete_task(conn, prerequisite, summary='Real prerequisite delivered.')
            resumed = tick(conn)
            assert [row[0] for row in resumed.spawned] == [feature]
            assert kb.get_task(conn, feature).current_run_id != old_run
            assert tick(conn).spawned == []
            assert kb.get_task(conn, held).status == 'blocked'
            assert len(kb.list_runs(conn, feature)) == 2
            assert len(kb.list_runs(conn, unrelated)) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.communicate(timeout=10)
