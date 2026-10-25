"""Conversation-owned Kanban dependencies for the existing goal wait barrier."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass
import logging
from pathlib import Path
import sqlite3

logger = logging.getLogger(__name__)


def _owner_sessions(session_id):
    from hermes_cli.goals import _get_session_db
    db = _get_session_db()
    return {session_id, *(db.get_compression_lineage(session_id) if db is not None else [])}


def _read_board(path):
    from hermes_cli.sqlite_safe_read import connect_tracked
    conn = connect_tracked(Path(path).as_uri() + '?mode=ro', uri=True, timeout=1, tracking_path=path)
    conn.row_factory = sqlite3.Row
    return closing(conn)


@dataclass(frozen=True)
class KanbanWait:
    profile_home: str
    db_path: str
    board: str
    task_id: str
    run_id: int
    session_id: str
    worker_pid: int
    worker_started_at: str | int

    @property
    def description(self):
        return f'Kanban {self.board}/{self.task_id} run {self.run_id}'

    def is_running(self, owner_session_id):
        from hermes_cli.kanban_db_dispatch import _worker_alive, UNVERIFIED_WORKER_FINGERPRINT
        from hermes_constants import get_hermes_home

        if (str(get_hermes_home().resolve()) != self.profile_home
                or self.session_id not in _owner_sessions(owner_session_id)
                or not self.worker_started_at or self.worker_started_at == UNVERIFIED_WORKER_FINGERPRINT):
            return False
        try:
            with _read_board(self.db_path) as conn:
                row = conn.execute(
                    "SELECT t.worker_pid, t.worker_started_at FROM tasks t "
                    "JOIN task_runs r ON r.id = t.current_run_id AND r.task_id = t.id "
                    "WHERE t.id = ? AND r.id = ? AND t.session_id = ? "
                    "AND t.status = 'running' AND r.status = 'running'",
                    (self.task_id, self.run_id, self.session_id),
                ).fetchone()
            return bool(row and row['worker_pid'] == self.worker_pid
                        and row['worker_started_at'] == self.worker_started_at
                        and _worker_alive(self.worker_pid, self.worker_started_at))
        except (OSError, sqlite3.Error):
            logger.warning('Goal dependency %s unavailable; its owner must reconcile', self.description, exc_info=True)
            return False


def gather_workers(owner_session_id):
    """Read existing boards only; another conversation's workers never enter the snapshot."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _worker_alive, UNVERIFIED_WORKER_FINGERPRINT
    from hermes_constants import get_hermes_home

    owners = sorted(_owner_sessions(owner_session_id))
    profile_home = str(get_hermes_home().resolve())
    found, seen = [], set()
    with kb.pin_first_board_resolution():
        boards = [(item['slug'], kb.kanban_db_path(item['slug']).resolve())
                  for item in kb.list_boards(include_archived=False)]
    for board, path in boards:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            with _read_board(path) as conn:
                rows = conn.execute(
                    "SELECT t.id, t.title, t.session_id, t.current_run_id, t.worker_pid, t.worker_started_at "
                    "FROM tasks t JOIN task_runs r ON r.id = t.current_run_id AND r.task_id = t.id "
                    f"WHERE t.session_id IN ({','.join('?' for _ in owners)}) "
                    "AND t.status = 'running' AND r.status = 'running' AND t.worker_pid IS NOT NULL", owners,
                ).fetchall()
            for row in rows:
                fingerprint = row['worker_started_at']
                if (not fingerprint or fingerprint == UNVERIFIED_WORKER_FINGERPRINT
                        or not _worker_alive(row['worker_pid'], fingerprint)):
                    continue
                wait = KanbanWait(profile_home, str(path), board, row['id'], row['current_run_id'],
                                  row['session_id'], row['worker_pid'], fingerprint)
                found.append({'pid': wait.worker_pid, 'status': 'running', 'owner_task_id': owner_session_id,
                              'command': f'{wait.description}: {row["title"]}', 'kanban_wait': asdict(wait)})
        except (OSError, sqlite3.Error):
            logger.warning('Goal worker snapshot for board %s failed', board, exc_info=True)
    return found
