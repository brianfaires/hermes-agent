"""State store for the Kanban Discord mirror daemon.

Owns two distinct things:

1. All persistent mirror state (``mirror.db``) — initiatives, members,
   posted notes, and reply-inbox receipts. Schema + accessors + mutators.
2. A strictly read-only snapshot loader over the board's ``kanban.db``.
   The mirror daemon must never write to the board DB — that's the
   dispatcher/worker's job. ``_connect_board_readonly`` opens the file
   with sqlite's ``mode=ro`` URI flag so any accidental write raises.

Follows the plain-SQL style of ``hermes_cli/kanban_db.py`` — no ORM.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Board snapshot (read-only)
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = {"done", "archived"}
_BLOCKED_STATUSES = {"blocked"}
_REVIEW_STATUSES = {"review"}
_ACTIVE_STATUSES = {"running", "ready", "todo", "scheduled", "triage"}


def is_terminal(status: str) -> bool:
    return (status or "").strip().lower() in _TERMINAL_STATUSES


def _status_class(status: str) -> str:
    """Collapse a raw task status into active|blocked|review|terminal."""
    s = (status or "").strip().lower()
    if s in _TERMINAL_STATUSES:
        return "terminal"
    if s in _BLOCKED_STATUSES:
        return "blocked"
    if s in _REVIEW_STATUSES:
        return "review"
    return "active"


@dataclass
class Card:
    id: str
    title: str | int | None
    body: str | int | None
    status: str | int | None
    priority: str | int | None
    assignee: str | int | None
    branch_name: str | int | None
    workspace_kind: str | int | None
    created_by: str | int | None
    created_at: str | int | None
    completed_at: str | int | None
    last_failure_error: str | int | None
    result: str | int | None


@dataclass
class BoardSnapshot:
    cards: dict[str, Card]
    children: dict[str, list[str]]
    parents: dict[str, list[str]]
    recent_comments: dict[str, list[dict]]
    recent_events: dict[str, list[dict]]
    owner_instructions: dict[str, list[dict]] = field(default_factory=dict)

    def active_roots(self) -> list[Card]:
        return [
            card
            for task_id, card in self.cards.items()
            if not is_terminal(card.status) and not self.parents.get(task_id)
        ]


def _card_from_row(row: sqlite3.Row) -> Card:
    return Card(
        id=row["id"],
        title=row["title"],
        body=row["body"],
        status=row["status"],
        priority=row["priority"],
        assignee=row["assignee"],
        branch_name=row["branch_name"],
        workspace_kind=row["workspace_kind"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        last_failure_error=row["last_failure_error"],
        result=row["result"],
    )


def _connect_board_readonly(board: str) -> sqlite3.Connection:
    from hermes_cli.kanban_db import kanban_db_path

    path = kanban_db_path(board)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _recent(con: sqlite3.Connection, table: str, cards: dict[str, Card]) -> dict[str, list[dict]]:
    if table == "task_comments":
        cols = "id, task_id, author, body, created_at"
        keys = ("id", "author", "body", "created_at")
    else:
        cols = "id, task_id, kind, payload, created_at"
        keys = ("id", "kind", "payload", "created_at")
    query = f"""
        SELECT {cols} FROM (
            SELECT {cols},
                   ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY id DESC) AS rn
            FROM {table}
        )
        WHERE rn <= 10
        ORDER BY task_id, id DESC
    """
    out: dict[str, list[dict]] = {}
    for row in con.execute(query):
        task_id = row["task_id"]
        if task_id not in cards:
            continue
        out.setdefault(task_id, []).append({k: row[k] for k in keys})
    return out


def load_board_snapshot(board: str) -> BoardSnapshot:
    con = _connect_board_readonly(board)
    try:
        cards = {r["id"]: _card_from_row(r) for r in con.execute("SELECT * FROM tasks")}
        children: dict[str, list[str]] = {}
        parents: dict[str, list[str]] = {}
        for e in con.execute("SELECT parent_id, child_id FROM task_links"):
            if e["parent_id"] in cards and e["child_id"] in cards:
                children.setdefault(e["parent_id"], []).append(e["child_id"])
                parents.setdefault(e["child_id"], []).append(e["parent_id"])
        recent_comments = _recent(con, "task_comments", cards)
        recent_events = _recent(con, "task_events", cards)
        owner_instructions: dict[str, list[dict]] = {}
        try:
            for row in con.execute("SELECT id,task_id,status FROM task_owner_instructions ORDER BY id"):
                if row["task_id"] in cards:
                    owner_instructions.setdefault(row["task_id"], []).append(dict(row))
        except sqlite3.OperationalError:
            pass
        return BoardSnapshot(cards, children, parents, recent_comments, recent_events, owner_instructions)
    finally:
        con.close()


def material_sig(card: Card, child_statuses: list[str]) -> str:
    """sha256 over title|body|status-class|sorted child ids+status-classes.

    Collapsing statuses to their class means heartbeat-ish status churn
    (e.g. running -> running after a retry) doesn't flag prose stale.
    """
    child_classes = sorted(_status_class(s) for s in child_statuses)
    payload = "|".join(
        [
            str(card.title or ""),
            str(card.body or ""),
            _status_class(str(card.status or "")),
            ",".join(child_classes),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Mirror state store (mirror.db)
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mirror_initiatives (
  id TEXT PRIMARY KEY,             -- "init_" + first root task id, or "digest"
  title TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'post',  -- post | digest
  thread_id TEXT,
  starter_message_id TEXT,
  brief TEXT,
  needs_you TEXT,
  blocked_reasons TEXT NOT NULL DEFAULT '{}',  -- JSON {task_id: short reason}
  published_hash TEXT,                         -- sha256 of last-published title+body+tags
  brief_stale INTEGER NOT NULL DEFAULT 1,
  brief_updated_at INTEGER,
  archived_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mirror_members (
  task_id TEXT PRIMARY KEY,
  initiative_id TEXT NOT NULL REFERENCES mirror_initiatives(id) ON DELETE CASCADE,
  last_status TEXT,
  last_sig TEXT
);
CREATE TABLE IF NOT EXISTS mirror_notes (
  note_key TEXT PRIMARY KEY,       -- e.g. "done:t_abc123" / "blocked:t_abc:2" — planner-provided idempotency key
  initiative_id TEXT NOT NULL,
  message_id TEXT,
  posted_at INTEGER NOT NULL
);
"""

RECEIPTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mirror_inbox_receipts (
  discord_message_id TEXT PRIMARY KEY,
  board_slug TEXT, forum_channel_id TEXT, thread_id TEXT, task_id TEXT,
  author_id TEXT, action TEXT,
  replied_to_message_id TEXT, replied_to_kanban_comment_id INTEGER,
  kanban_comment_id INTEGER, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mirror_reaction_states (
  reaction_key TEXT PRIMARY KEY,
  generation INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL
);
"""

SCHEMA_SQL += RECEIPTS_SCHEMA_SQL


@dataclass
class MemberState:
    task_id: str
    last_status: str | None
    last_sig: str | None


@dataclass
class Initiative:
    id: str
    title: str
    kind: str
    thread_id: str | None
    starter_message_id: str | None
    brief: str | None
    needs_you: str | None
    blocked_reasons: dict[str, str]
    published_hash: str | None
    brief_stale: bool
    brief_updated_at: int | None
    archived_at: int | None
    created_at: int
    updated_at: int
    members: dict[str, MemberState] = field(default_factory=dict)


def mirror_db_path(board: str) -> Path:
    from hermes_cli.kanban_db import board_dir

    return board_dir(board) / "mirror.db"


def connect_mirror(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the daemon opens this connection on the event
    # loop thread but runs every accessor/mutator via asyncio.to_thread, whose
    # pool threads vary call-to-call. Access is serialized (each call is
    # awaited before the next starts), so cross-thread reuse is safe.
    conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


def _now() -> int:
    return int(time.time())


def _initiative_from_row(row: sqlite3.Row) -> Initiative:
    return Initiative(
        id=row["id"],
        title=row["title"],
        kind=row["kind"],
        thread_id=row["thread_id"],
        starter_message_id=row["starter_message_id"],
        brief=row["brief"],
        needs_you=row["needs_you"],
        blocked_reasons=json.loads(row["blocked_reasons"] or "{}"),
        published_hash=row["published_hash"],
        brief_stale=bool(row["brief_stale"]),
        brief_updated_at=row["brief_updated_at"],
        archived_at=row["archived_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def load_mirror_state(conn: sqlite3.Connection) -> dict[str, Initiative]:
    initiatives: dict[str, Initiative] = {}
    for row in conn.execute("SELECT * FROM mirror_initiatives"):
        initiatives[row["id"]] = _initiative_from_row(row)
    for row in conn.execute("SELECT * FROM mirror_members"):
        init = initiatives.get(row["initiative_id"])
        if init is None:
            continue
        init.members[row["task_id"]] = MemberState(
            task_id=row["task_id"],
            last_status=row["last_status"],
            last_sig=row["last_sig"],
        )
    return initiatives


def load_note_keys(conn: sqlite3.Connection) -> set[str]:
    return {row["note_key"] for row in conn.execute("SELECT note_key FROM mirror_notes")}


def get_digest(conn: sqlite3.Connection) -> Initiative | None:
    row = conn.execute(
        "SELECT * FROM mirror_initiatives WHERE kind = 'digest' LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    init = _initiative_from_row(row)
    for m in conn.execute(
        "SELECT * FROM mirror_members WHERE initiative_id = ?", (init.id,)
    ):
        init.members[m["task_id"]] = MemberState(
            task_id=m["task_id"], last_status=m["last_status"], last_sig=m["last_sig"]
        )
    return init


# --- mutators ---------------------------------------------------------------


def create_initiative(
    conn: sqlite3.Connection, initiative_id: str, title: str, kind: str = "post"
) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO mirror_initiatives (id, title, kind, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET title = excluded.title, updated_at = excluded.updated_at
        """,
        (initiative_id, title, kind, now, now),
    )
    conn.commit()


def add_member(conn: sqlite3.Connection, initiative_id: str, task_id: str) -> None:
    conn.execute(
        """
        INSERT INTO mirror_members (task_id, initiative_id, last_status, last_sig)
        VALUES (?, ?, NULL, NULL)
        ON CONFLICT(task_id) DO UPDATE SET initiative_id = excluded.initiative_id
        """,
        (task_id, initiative_id),
    )
    conn.execute(
        "UPDATE mirror_initiatives SET updated_at = ? WHERE id = ?",
        (_now(), initiative_id),
    )
    conn.commit()


def remove_member(conn: sqlite3.Connection, task_id: str) -> None:
    row = conn.execute(
        "SELECT initiative_id FROM mirror_members WHERE task_id = ?", (task_id,)
    ).fetchone()
    conn.execute("DELETE FROM mirror_members WHERE task_id = ?", (task_id,))
    if row is not None:
        conn.execute(
            "UPDATE mirror_initiatives SET updated_at = ? WHERE id = ?",
            (_now(), row["initiative_id"]),
        )
    conn.commit()


def set_thread(
    conn: sqlite3.Connection, initiative_id: str, thread_id: str, starter_message_id: str
) -> None:
    conn.execute(
        """
        UPDATE mirror_initiatives
        SET thread_id = ?, starter_message_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (thread_id, starter_message_id, _now(), initiative_id),
    )
    conn.commit()


def set_prose(
    conn: sqlite3.Connection,
    initiative_id: str,
    brief: str,
    needs_you: str,
    blocked_reasons: dict[str, str] | None = None,
) -> None:
    now = _now()
    conn.execute(
        """
        UPDATE mirror_initiatives
        SET brief = ?, needs_you = ?, blocked_reasons = ?,
            brief_stale = 0, brief_updated_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            brief,
            needs_you,
            json.dumps(blocked_reasons or {}),
            now,
            now,
            initiative_id,
        ),
    )
    conn.commit()


def mark_brief_stale(conn: sqlite3.Connection, initiative_id: str) -> None:
    conn.execute(
        "UPDATE mirror_initiatives SET brief_stale = 1, updated_at = ? WHERE id = ?",
        (_now(), initiative_id),
    )
    conn.commit()


def set_member_seen(conn: sqlite3.Connection, task_id: str, status: str, sig: str) -> None:
    conn.execute(
        """
        UPDATE mirror_members SET last_status = ?, last_sig = ? WHERE task_id = ?
        """,
        (status, sig, task_id),
    )
    conn.commit()


def set_initiative_title(conn: sqlite3.Connection, initiative_id: str, title: str) -> None:
    conn.execute(
        "UPDATE mirror_initiatives SET title = ?, updated_at = ? WHERE id = ?",
        (title, _now(), initiative_id),
    )
    conn.commit()


def set_archived(conn: sqlite3.Connection, initiative_id: str, archived_at: int) -> None:
    conn.execute(
        "UPDATE mirror_initiatives SET archived_at = ?, updated_at = ? WHERE id = ?",
        (archived_at, _now(), initiative_id),
    )
    conn.commit()


def clear_archived(conn: sqlite3.Connection, initiative_id: str) -> None:
    conn.execute(
        "UPDATE mirror_initiatives SET archived_at = NULL, updated_at = ? WHERE id = ?",
        (_now(), initiative_id),
    )
    conn.commit()


def record_note(
    conn: sqlite3.Connection, initiative_id: str, note_key: str, message_id: str
) -> None:
    conn.execute(
        """
        INSERT INTO mirror_notes (note_key, initiative_id, message_id, posted_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(note_key) DO UPDATE SET
            initiative_id = excluded.initiative_id,
            message_id = excluded.message_id,
            posted_at = excluded.posted_at
        """,
        (note_key, initiative_id, message_id, _now()),
    )
    conn.commit()


def note_exists(conn: sqlite3.Connection, note_key: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM mirror_notes WHERE note_key = ?", (note_key,)
    ).fetchone()
    return row is not None


def resolve_thread_task(
    mirror_path: Path, forum_channel_id: str, thread_id: str
) -> tuple[str, str] | None:
    """Resolve a Discord forum thread back to its primary task + board.

    ``forum_channel_id`` is accepted for interface symmetry with the
    caller's lookup key but isn't itself stored on mirror_initiatives in
    this schema; the thread_id alone identifies the initiative within a
    given mirror.db (one board per mirror.db file). The board slug is
    derived from the mirror.db's parent directory name (matches
    ``board_dir(board) / "mirror.db"``).

    Gracefully returns ``None`` when no mirror state exists yet — a reply
    can arrive before the daemon has ever created mirror.db for a board.
    Opened read-only so a lookup can never create the file.
    """
    if not mirror_path.exists():
        return None
    conn = sqlite3.connect(f"file:{mirror_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        try:
            row = conn.execute(
                "SELECT id FROM mirror_initiatives WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            # Empty/uninitialized mirror.db (no such table) — treat as no match.
            return None
        if row is None:
            return None
        initiative_id = row["id"]
        member = conn.execute(
            """
            SELECT task_id FROM mirror_members
            WHERE initiative_id = ?
            ORDER BY rowid ASC
            LIMIT 1
            """,
            (initiative_id,),
        ).fetchone()
        if member is None:
            return None
        board_slug = mirror_path.parent.name
        return (member["task_id"], board_slug)
    finally:
        conn.close()


# --- reply-inbox receipts ----------------------------------------------------


def ensure_receipts(conn: sqlite3.Connection) -> None:
    conn.executescript(RECEIPTS_SCHEMA_SQL)
    conn.commit()


def receipt_exists(conn: sqlite3.Connection, discord_message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM mirror_inbox_receipts WHERE discord_message_id = ?",
        (discord_message_id,),
    ).fetchone()
    return row is not None


def record_receipt(conn: sqlite3.Connection, **fields) -> None:
    columns = [
        "discord_message_id",
        "board_slug",
        "forum_channel_id",
        "thread_id",
        "task_id",
        "author_id",
        "action",
        "replied_to_message_id",
        "replied_to_kanban_comment_id",
        "kanban_comment_id",
        "created_at",
    ]
    values = {c: fields.get(c) for c in columns}
    if values["created_at"] is None:
        values["created_at"] = _now()
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"""
        INSERT INTO mirror_inbox_receipts ({", ".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(discord_message_id) DO NOTHING
        """,
        [values[c] for c in columns],
    )
    conn.commit()


def reaction_generation(conn: sqlite3.Connection, reaction_key: str) -> int:
    row = conn.execute(
        "SELECT generation FROM mirror_reaction_states WHERE reaction_key=?",
        (reaction_key,),
    ).fetchone()
    return int(row["generation"]) if row is not None else 0


def mark_reaction_active(conn: sqlite3.Connection, reaction_key: str) -> None:
    conn.execute(
        """INSERT INTO mirror_reaction_states(reaction_key,generation,active,updated_at)
           VALUES (?,0,1,?)
           ON CONFLICT(reaction_key) DO UPDATE SET active=1,updated_at=excluded.updated_at""",
        (reaction_key, _now()),
    )


def mark_reaction_removed(conn: sqlite3.Connection, reaction_key: str) -> bool:
    """Atomically advance the lifecycle generation and clear its active receipt."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        existed = receipt_exists(conn, reaction_key)
        conn.execute(
            """INSERT INTO mirror_reaction_states(reaction_key,generation,active,updated_at)
               VALUES (?,1,0,?)
               ON CONFLICT(reaction_key) DO UPDATE SET
                 generation=mirror_reaction_states.generation+1,
                 active=0,
                 updated_at=excluded.updated_at""",
            (reaction_key, _now()),
        )
        conn.execute(
            "DELETE FROM mirror_inbox_receipts WHERE discord_message_id=?",
            (reaction_key,),
        )
        conn.commit()
        return existed
    except Exception:
        conn.rollback()
        raise


def find_receipt_comment_id(conn: sqlite3.Connection, discord_message_id: str) -> int | None:
    row = conn.execute(
        "SELECT kanban_comment_id FROM mirror_inbox_receipts WHERE discord_message_id = ?",
        (discord_message_id,),
    ).fetchone()
    if row is None:
        return None
    return row["kanban_comment_id"]
