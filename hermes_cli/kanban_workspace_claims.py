"""Workspace exclusion and atomic graph gates for the native Kanban queue."""
from __future__ import annotations

from contextlib import contextmanager
import errno
from pathlib import Path
import sqlite3
from typing import Optional


class WorkspacePlanError(ValueError):
    """The candidate card cannot name a usable workspace."""


def child_workspace(root, child: dict) -> tuple[str, Optional[str]]:
    """Scratch/worktree children receive independent native destinations."""
    root_kind = root['workspace_kind'] or 'scratch'
    kind = child.get('workspace_kind') or root_kind
    path = child.get('workspace_path')
    if not path and kind == root_kind and kind == 'dir':
        path = root['workspace_path']
    return kind, path


def _normalized_path(value: str) -> Path:
    from hermes_cli.kanban_db_workspace import _path_key

    if not value:
        raise ValueError('workspace_path must be absolute')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('workspace_path must be absolute')
    return Path(_path_key(path.resolve()))


def _claim_path(conn: sqlite3.Connection, row) -> Path:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.kanban_db_workspace import worktree_destination

    db_file = _main_db_file(conn)
    db = Path(db_file).resolve() if db_file else None
    board = kb.get_current_board()
    if db == (kb.kanban_home() / 'kanban.db').resolve():
        board = 'default'
    elif db is not None and db.parent.parent == kb.boards_root().resolve():
        board = db.parent.name
    if row['workspace_kind'] == 'worktree':
        return _normalized_path(str(worktree_destination(kb.Task.from_row(row), board=board)[0]))
    if row['workspace_kind'] == 'scratch' and not row['workspace_path']:
        return _normalized_path(str(kb.workspaces_root(board=board) / row['id']))
    return _normalized_path(row['workspace_path'])


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _depends_on(children: list[dict], child: int, ancestor: int) -> bool:
    pending = list(children[child].get('parents') or [])
    visited = set()
    while pending:
        current = pending.pop()
        if current == ancestor:
            return True
        if current not in visited:
            visited.add(current)
            pending.extend(children[current].get('parents') or [])
    return False


def validate_parallel_workspaces(root, children: list[dict]) -> None:
    """Allow ordered directory reuse; refuse overlapping parallel writers."""
    paths = []
    for index, child in enumerate(children):
        kind, path = child_workspace(root, child)
        if not path:
            continue
        # Worktree children without an id have independent native defaults;
        # explicit destinations are enforced again when the card is claimed.
        if kind == 'worktree':
            continue
        resolved = _normalized_path(path)
        for other_index, other_path in paths:
            if (_overlap(resolved, other_path)
                    and not _depends_on(children, index, other_index)
                    and not _depends_on(children, other_index, index)):
                raise ValueError(f'child[{index}] workspace overlaps parallel child[{other_index}]')
        paths.append((index, resolved))


def inherit_prerequisites(conn: sqlite3.Connection, root_id: str,
                          child_ids: list[str], children: list[dict]) -> None:
    """Persist external prerequisites on graph entrypoints before root fan-in."""
    from hermes_cli import kanban_db as kb

    parents = conn.execute('SELECT parent_id FROM task_links WHERE child_id = ?', (root_id,)).fetchall()
    for index, child in enumerate(children):
        if child.get('parents'):
            continue
        for row in parents:
            parent = row['parent_id']
            kb._link(conn, parent, child_ids[index])
            kb._append_event(conn, child_ids[index], 'linked',
                             {'parent': parent, 'child': child_ids[index], 'propagated_from': root_id})


@contextmanager
def workspace_claim_slot():
    """Serialize claims across this fleet's board DBs through their commits."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import _try_lock_nb, _unlock

    path = kb.kanban_home() / 'kanban' / '.workspace-claims.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        try:
            acquired = _try_lock_nb(handle)
        except OSError as exc:
            contended = exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK} or (
                kb._IS_WINDOWS and exc.errno in {errno.EACCES, errno.EDEADLK})
            if not contended:
                kb._log.warning(
                    'Kanban workspace claims deferred: cannot lock %s: %s. '
                    'Check filesystem locking support and permissions.', path, exc)
            acquired = False
        try:
            yield acquired
        finally:
            if acquired:
                _unlock(handle)


def _running_overlap(conn: sqlite3.Connection, path: Path, task_id: Optional[str]) -> Optional[str]:
    rows = conn.execute("SELECT * FROM tasks WHERE status = 'running'")
    for row in rows:
        try:
            other = _claim_path(conn, row)
        # Older supported Python versions report symlink loops as RuntimeError.
        except (OSError, ValueError, RuntimeError) as exc:
            return f"{row['id']}: workspace unavailable: {exc}"
        if row['id'] != task_id and other is not None and _overlap(path, other):
            return row['id']
    return None


def workspace_conflict(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Read native running claims; an unreadable sibling board cannot prove exclusion."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_workspace import _sibling_board_db_files
    from hermes_cli.sqlite_safe_read import connect_tracked

    row = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not row:
        return None
    try:
        path = _claim_path(conn, row)
    except (OSError, ValueError, RuntimeError) as exc:
        raise WorkspacePlanError(str(exc)) from exc
    conflict = _running_overlap(conn, path, task_id)
    if conflict:
        return conflict
    try:
        for db in _sibling_board_db_files(conn):
            other = connect_tracked(db.as_uri() + '?mode=ro', uri=True, timeout=1, tracking_path=db)
            try:
                other.row_factory = sqlite3.Row
                conflict = _running_overlap(other, path, None)
                if conflict:
                    return f'{db}:{conflict}'
            finally:
                other.close()
    except (OSError, sqlite3.Error, ValueError) as exc:
        kb._log.warning('Kanban workspace claim deferred: sibling board cannot be read: %s', exc)
        return 'unreadable sibling board'
    return None


def claim_and_open_run(
    conn: sqlite3.Connection, task_id: str, source_status: str, lock: str, expires: int, now: int,
    *, event_extra: Optional[dict] = None,
) -> Optional[int]:
    """CAS ``source_status -> running``, open a run row, emit ``claimed``; None
    when the CAS lost. Caller holds the txn."""
    from hermes_cli import kanban_db as kb

    if kb._has_sticky_block(conn, task_id):
        return None
    conflict = workspace_conflict(conn, task_id)
    if conflict:
        kb._append_event(conn, task_id, 'claim_rejected',
                         {'reason': 'workspace_overlap', 'owner': conflict})
        return None
    cur = conn.execute(
        f"""
        UPDATE tasks
           SET status        = 'running',
               claim_lock    = ?,
               claim_expires = ?,
               started_at    = COALESCE(started_at, ?)
         WHERE id = ?
           AND status = '{source_status}'
           AND claim_lock IS NULL
        """,
        (lock, expires, now, task_id),
    )
    if cur.rowcount != 1:
        return None
    trow = conn.execute(
        "SELECT assignee, max_runtime_seconds, current_step_key "
        "FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    run_cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key, status,
            claim_lock, claim_expires, max_runtime_seconds,
            started_at
        ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
        """,
        (
            task_id, trow["assignee"] if trow else None, trow["current_step_key"] if trow else None,
            lock, expires, trow["max_runtime_seconds"] if trow else None, now,
        ),
    )
    run_id = run_cur.lastrowid
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
    kb._append_event(
        conn, task_id, "claimed",
        {"lock": lock, "expires": expires, "run_id": run_id, **(event_extra or {})}, run_id=run_id,
    )
    return run_id


def claim_for_dispatch(conn, row, lane, ttl_seconds, auto_blocked):
    """Keep one candidate's planning failure from stopping unrelated cards or a newer owner."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _record_task_failure

    claim = kb.claim_review_task if lane == 'review' else kb.claim_task
    try:
        return claim(conn, row['id'], ttl_seconds=ttl_seconds)
    except WorkspacePlanError as exc:
        if _record_task_failure(conn, row['id'], f"Workspace unavailable: {exc}",
                                outcome='spawn_failed', force_trip=True,
                                expected_attempt=(lane, row['current_run_id'])):
            auto_blocked.append(row['id'])
        return None
