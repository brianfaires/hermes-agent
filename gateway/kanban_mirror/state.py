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
CREATE TABLE IF NOT EXISTS mirror_binding_epochs (
  binding_key TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL,
  board_slug TEXT NOT NULL,
  task_id TEXT NOT NULL,
  sequence INTEGER NOT NULL CHECK (sequence > 0),
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  transition_message_id TEXT,
  starter_revision_hash TEXT,
  state TEXT NOT NULL DEFAULT 'open',
  UNIQUE(thread_id, sequence),
  CHECK ((state = 'open' AND ended_at IS NULL) OR
         (state != 'open' AND ended_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mirror_binding_epochs_one_open_thread
ON mirror_binding_epochs(thread_id) WHERE state = 'open';
CREATE INDEX IF NOT EXISTS idx_mirror_binding_epochs_task
ON mirror_binding_epochs(board_slug, task_id);
CREATE TABLE IF NOT EXISTS mirror_binding_transitions (
  transition_key TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
  old_binding_key TEXT NOT NULL, new_binding_key TEXT NOT NULL UNIQUE,
  old_card_metadata TEXT NOT NULL, new_card_metadata TEXT NOT NULL,
  transition_payload TEXT NOT NULL, starter_payload TEXT NOT NULL,
  frozen_hash TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('prepared','message_confirmed','starter_verified')),
  transition_message_id TEXT UNIQUE, prepared_at INTEGER NOT NULL,
  confirmed_at INTEGER, starter_verified_at INTEGER,
  FOREIGN KEY(old_binding_key) REFERENCES mirror_binding_epochs(binding_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mirror_binding_transitions_pending_thread
ON mirror_binding_transitions(thread_id) WHERE state = 'prepared';
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
CREATE TABLE IF NOT EXISTS mirror_conversation_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  discord_message_id TEXT NOT NULL UNIQUE,
  thread_id TEXT NOT NULL,
  binding_key TEXT,
  event_class TEXT NOT NULL,
  author_label TEXT NOT NULL,
  content TEXT NOT NULL,
  replied_to_message_id TEXT,
  discord_created_at INTEGER,
  recorded_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mirror_conversation_events_thread_binding
ON mirror_conversation_events(thread_id, binding_key, id);
CREATE TABLE IF NOT EXISTS mirror_conversation_deliveries (
  operation_id TEXT PRIMARY KEY,
  trigger_discord_message_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  kanban_comment_id INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  delivered_at INTEGER
);
CREATE TABLE IF NOT EXISTS mirror_conversation_delivery_items (
  operation_id TEXT NOT NULL,
  event_id INTEGER NOT NULL,
  PRIMARY KEY (operation_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_mirror_conversation_delivery_items_event
ON mirror_conversation_delivery_items(event_id);
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


@dataclass(frozen=True)
class BindingEpoch:
    binding_key: str
    thread_id: str
    board_slug: str
    task_id: str
    sequence: int
    started_at: int
    ended_at: int | None
    transition_message_id: str | None
    starter_revision_hash: str | None
    state: str


@dataclass(frozen=True)
class BindingTransition:
    transition_key: str
    thread_id: str
    old_binding_key: str
    new_binding_key: str
    old_card_metadata: dict
    new_card_metadata: dict
    transition_payload: dict
    starter_payload: dict
    frozen_hash: str
    state: str
    transition_message_id: str | None
    prepared_at: int
    confirmed_at: int | None
    starter_verified_at: int | None


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


def _binding_from_row(row: sqlite3.Row) -> BindingEpoch:
    return BindingEpoch(
        binding_key=str(row["binding_key"]), thread_id=str(row["thread_id"]),
        board_slug=str(row["board_slug"]), task_id=str(row["task_id"]),
        sequence=int(row["sequence"]), started_at=int(row["started_at"]),
        ended_at=row["ended_at"], transition_message_id=row["transition_message_id"],
        starter_revision_hash=row["starter_revision_hash"], state=str(row["state"]),
    )


def active_thread_binding(conn: sqlite3.Connection, thread_id: str) -> BindingEpoch | None:
    """Return the sole open epoch, failing closed if state is ambiguous."""
    rows = conn.execute(
        "SELECT * FROM mirror_binding_epochs WHERE thread_id=? AND state='open' ORDER BY sequence",
        (str(thread_id),),
    ).fetchall()
    return _binding_from_row(rows[0]) if len(rows) == 1 else None


def _canonical(value: dict) -> str:
    if not isinstance(value, dict):
        raise ValueError("transition values must be objects")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _transition_from_row(row: sqlite3.Row) -> BindingTransition:
    return BindingTransition(
        transition_key=str(row["transition_key"]), thread_id=str(row["thread_id"]),
        old_binding_key=str(row["old_binding_key"]), new_binding_key=str(row["new_binding_key"]),
        old_card_metadata=json.loads(row["old_card_metadata"]), new_card_metadata=json.loads(row["new_card_metadata"]),
        transition_payload=json.loads(row["transition_payload"]), starter_payload=json.loads(row["starter_payload"]),
        frozen_hash=str(row["frozen_hash"]), state=str(row["state"]),
        transition_message_id=row["transition_message_id"], prepared_at=int(row["prepared_at"]),
        confirmed_at=row["confirmed_at"], starter_verified_at=row["starter_verified_at"])


def get_binding_transition(conn: sqlite3.Connection, transition_key: str) -> BindingTransition | None:
    row = conn.execute("SELECT * FROM mirror_binding_transitions WHERE transition_key=?", (transition_key,)).fetchone()
    return _transition_from_row(row) if row is not None else None


def pending_binding_transition(conn: sqlite3.Connection, thread_id: str) -> BindingTransition | None:
    rows = conn.execute("SELECT * FROM mirror_binding_transitions WHERE thread_id=? AND state='prepared'", (str(thread_id),)).fetchall()
    return _transition_from_row(rows[0]) if len(rows) == 1 else None


def prepare_binding_transition(conn: sqlite3.Connection, *, transition_key: str, thread_id: str,
                               old_card_metadata: dict, new_card_metadata: dict,
                               transition_payload: dict, starter_payload: dict) -> BindingTransition:
    """Freeze a recoverable transition while the old epoch remains authoritative."""
    key, thread = str(transition_key).strip(), str(thread_id).strip()
    if not key or not thread:
        raise ValueError("transition_key and thread_id are required")
    values = tuple(_canonical(v) for v in (old_card_metadata, new_card_metadata, transition_payload, starter_payload))
    frozen_hash = hashlib.sha256("\0".join(values).encode()).hexdigest()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM mirror_binding_transitions WHERE transition_key=?", (key,)).fetchone()
        if row is not None:
            result = _transition_from_row(row)
            if result.thread_id != thread or result.frozen_hash != frozen_hash:
                raise ValueError("transition retry does not match frozen state")
            conn.commit(); return result
        rows = conn.execute("SELECT * FROM mirror_binding_epochs WHERE thread_id=? AND state='open'", (thread,)).fetchall()
        if len(rows) != 1:
            raise ValueError("thread does not have exactly one authoritative binding")
        old = _binding_from_row(rows[0])
        if (str(old_card_metadata.get("board_slug", "")), str(old_card_metadata.get("task_id", ""))) != (old.board_slug, old.task_id):
            raise ValueError("old card metadata does not match authoritative binding")
        new_board, new_task = str(new_card_metadata.get("board_slug", "")), str(new_card_metadata.get("task_id", ""))
        if not new_board or not new_task or (new_board, new_task) == (old.board_slug, old.task_id):
            raise ValueError("new card metadata is missing or unchanged")
        new_key = f"binding:{thread}:{old.sequence + 1}"
        conn.execute("""INSERT INTO mirror_binding_transitions
            (transition_key,thread_id,old_binding_key,new_binding_key,old_card_metadata,new_card_metadata,
             transition_payload,starter_payload,frozen_hash,state,prepared_at)
            VALUES (?,?,?,?,?,?,?,?,?,'prepared',?)""", (key, thread, old.binding_key, new_key, *values, frozen_hash, _now()))
        result = get_binding_transition(conn, key)
        conn.commit(); return result
    except Exception:
        conn.rollback(); raise


def confirm_binding_transition(conn: sqlite3.Connection, transition_key: str, transition_message_id: str) -> BindingTransition:
    """Atomically close old/open successor only after a Discord message confirmation."""
    message_id = str(transition_message_id).strip()
    if not message_id:
        raise ValueError("transition_message_id is required")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM mirror_binding_transitions WHERE transition_key=?", (transition_key,)).fetchone()
        if row is None:
            raise ValueError("unknown transition")
        transition = _transition_from_row(row)
        if transition.state != "prepared":
            if transition.transition_message_id != message_id:
                raise ValueError("transition was confirmed with a different message")
            conn.commit(); return transition
        rows = conn.execute("SELECT * FROM mirror_binding_epochs WHERE thread_id=? AND state='open'", (transition.thread_id,)).fetchall()
        if len(rows) != 1 or rows[0]["binding_key"] != transition.old_binding_key:
            raise ValueError("authoritative binding changed or is ambiguous")
        old, now = _binding_from_row(rows[0]), _now(); new = transition.new_card_metadata
        conn.execute("UPDATE mirror_binding_epochs SET state='closed',ended_at=?,transition_message_id=? WHERE binding_key=?", (now, message_id, old.binding_key))
        conn.execute("""INSERT INTO mirror_binding_epochs
            (binding_key,thread_id,board_slug,task_id,sequence,started_at,state) VALUES (?,?,?,?,?,?,'open')""",
            (transition.new_binding_key, transition.thread_id, str(new["board_slug"]), str(new["task_id"]), old.sequence + 1, now))
        conn.execute("UPDATE mirror_binding_transitions SET state='message_confirmed',transition_message_id=?,confirmed_at=? WHERE transition_key=? AND state='prepared'", (message_id, now, transition_key))
        result = get_binding_transition(conn, transition_key)
        conn.commit(); return result
    except Exception:
        conn.rollback(); raise


def authorize_starter_update(conn: sqlite3.Connection, transition_key: str) -> tuple[dict, str]:
    transition = get_binding_transition(conn, transition_key)
    if transition is None or transition.state not in {"message_confirmed", "starter_verified"}:
        raise ValueError("starter update is not authorized")
    active = active_thread_binding(conn, transition.thread_id)
    if active is None or active.binding_key != transition.new_binding_key:
        raise ValueError("successor binding is not authoritative")
    return transition.starter_payload, hashlib.sha256(_canonical(transition.starter_payload).encode()).hexdigest()


def verify_starter_revision(conn: sqlite3.Connection, transition_key: str, revision_hash: str) -> BindingTransition:
    """Capture a verified live revision hash; retries are idempotent."""
    _, expected = authorize_starter_update(conn, transition_key)
    if not revision_hash or revision_hash != expected:
        raise ValueError("starter revision does not match frozen payload")
    conn.execute("BEGIN IMMEDIATE")
    try:
        transition = get_binding_transition(conn, transition_key)
        row = conn.execute("SELECT starter_revision_hash FROM mirror_binding_epochs WHERE binding_key=? AND state='open'", (transition.new_binding_key,)).fetchone()
        if row is None or (row[0] is not None and row[0] != revision_hash):
            raise ValueError("starter revision state is ambiguous")
        now = _now()
        conn.execute("UPDATE mirror_binding_epochs SET starter_revision_hash=? WHERE binding_key=?", (revision_hash, transition.new_binding_key))
        conn.execute("UPDATE mirror_binding_transitions SET state='starter_verified',starter_verified_at=COALESCE(starter_verified_at,?) WHERE transition_key=?", (now, transition_key))
        result = get_binding_transition(conn, transition_key)
        conn.commit(); return result
    except Exception:
        conn.rollback(); raise


def backfill_legacy_bindings(conn: sqlite3.Connection, board_slug: str) -> int:
    """Idempotently turn unambiguous one-card thread mappings into epoch one."""
    board = str(board_slug or "").strip()
    if not board:
        raise ValueError("board_slug is required")
    conn.execute("BEGIN IMMEDIATE")
    try:
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO mirror_binding_epochs
              (binding_key,thread_id,board_slug,task_id,sequence,started_at,state)
            SELECT 'binding:' || i.thread_id || ':1', i.thread_id, ?, MIN(m.task_id),
                   1, i.created_at, 'open'
            FROM mirror_initiatives i JOIN mirror_members m ON m.initiative_id=i.id
            WHERE i.kind='post' AND i.thread_id IS NOT NULL AND i.thread_id!=''
              AND NOT EXISTS (SELECT 1 FROM mirror_binding_epochs b WHERE b.thread_id=i.thread_id)
            GROUP BY i.id, i.thread_id, i.created_at HAVING COUNT(m.task_id)=1
            """,
            (board,),
        )
        inserted = conn.total_changes - before
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise


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
            has_epochs = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mirror_binding_epochs'"
            ).fetchone()
            if has_epochs:
                epoch_rows = conn.execute(
                    "SELECT task_id,board_slug,state FROM mirror_binding_epochs WHERE thread_id=?",
                    (thread_id,),
                ).fetchall()
                if epoch_rows:
                    open_rows = [row for row in epoch_rows if row["state"] == "open"]
                    if len(open_rows) != 1:
                        return None
                    return (open_rows[0]["task_id"], open_rows[0]["board_slug"])
            initiatives = conn.execute(
                "SELECT id FROM mirror_initiatives WHERE thread_id = ?", (thread_id,)
            ).fetchall()
        except sqlite3.OperationalError:
            # Empty/uninitialized mirror.db (no such table) — treat as no match.
            return None
        if len(initiatives) != 1:
            return None
        initiative_id = initiatives[0]["id"]
        members = conn.execute(
            """
            SELECT task_id FROM mirror_members
            WHERE initiative_id = ?
            ORDER BY rowid ASC
            LIMIT 2
            """,
            (initiative_id,),
        ).fetchall()
        if len(members) != 1:
            return None
        board_slug = mirror_path.parent.name
        return (members[0]["task_id"], board_slug)
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
