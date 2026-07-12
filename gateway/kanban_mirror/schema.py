"""Single transactional schema boundary for the conversation-router mirror DB.

All mirror writers must pass through :func:`initialize_mirror_schema` before
use.  Migrations are deliberately additive: legacy tables and rows are never
rebuilt or removed.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable

MIRROR_SCHEMA_VERSION = 1


class MirrorSchemaError(RuntimeError):
    """The mirror database cannot safely be used by this release."""


# Columns added after the original mirror tables shipped.  CREATE TABLE IF NOT
# EXISTS does not upgrade an existing table, so keep additive upgrades explicit.
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "mirror_initiatives": {
        "kind": "TEXT NOT NULL DEFAULT 'post'", "thread_id": "TEXT",
        "starter_message_id": "TEXT", "brief": "TEXT", "needs_you": "TEXT",
        "blocked_reasons": "TEXT NOT NULL DEFAULT '{}'", "published_hash": "TEXT",
        "brief_stale": "INTEGER NOT NULL DEFAULT 1", "brief_updated_at": "INTEGER",
        "archived_at": "INTEGER", "updated_at": "INTEGER NOT NULL DEFAULT 0",
    },
    "mirror_conversation_events": {"binding_key": "TEXT", "replied_to_message_id": "TEXT", "discord_created_at": "INTEGER"},
    "mirror_discord_thread_cursors": {"backlog_limited": "INTEGER NOT NULL DEFAULT 0"},
    "mirror_discord_outbox": {
        "next_attempt_at": "INTEGER", "lease_owner": "TEXT", "lease_expires_at": "INTEGER",
        "confirmation_needed_at": "INTEGER", "quarantined_at": "INTEGER",
    },
}


def _statements(sql: str) -> list[str]:
    # Mirror DDL contains no triggers or semicolons in literals.
    return [part.strip() for part in sql.split(";") if part.strip()]


def _all_ddl() -> tuple[list[str], list[str]]:
    # Imports are local to avoid making state/outbox/recovery import order part
    # of the schema contract.
    from .outbox import OUTBOX_SCHEMA_SQL
    from .recovery import SQL as RECOVERY_SCHEMA_SQL
    from .state import SCHEMA_SQL

    tables: list[str] = []
    indexes: list[str] = []
    for statement in _statements(SCHEMA_SQL + OUTBOX_SCHEMA_SQL + RECOVERY_SCHEMA_SQL):
        (indexes if statement.upper().startswith("CREATE INDEX") or statement.upper().startswith("CREATE UNIQUE INDEX") else tables).append(statement)
    return tables, indexes


def initialize_mirror_schema(
    conn: sqlite3.Connection,
    *,
    _after_step: Callable[[str], None] | None = None,
) -> int:
    """Atomically create/upgrade and validate every mirror persistence table.

    ``BEGIN IMMEDIATE`` serializes concurrent processes.  SQLite transactional
    DDL means an exception (including an interrupted migration) leaves no
    partial schema.  ``_after_step`` is an internal fault-injection seam.
    """
    if conn.in_transaction:
        raise MirrorSchemaError("mirror schema initialization requires an idle connection")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS mirror_schema_version (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)"
        )
        row = conn.execute("SELECT version FROM mirror_schema_version WHERE singleton=1").fetchone()
        if row is not None and int(row[0]) > MIRROR_SCHEMA_VERSION:
            raise MirrorSchemaError(
                f"mirror schema version {row[0]} is newer than supported version {MIRROR_SCHEMA_VERSION}; upgrade Hermes"
            )
        tables, indexes = _all_ddl()
        for statement in tables:
            conn.execute(statement)
        if _after_step:
            _after_step("tables")
        for table, additions in _ADDITIVE_COLUMNS.items():
            existing = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, declaration in additions.items():
                if name not in existing:
                    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {declaration}')
        if _after_step:
            _after_step("columns")
        for statement in indexes:
            conn.execute(statement)
        # Recovery ownership belongs to this migration boundary too: a legacy
        # prepared transition must be visible to a worker immediately after
        # initialization, not only after a later lazy recovery setup call.
        conn.execute(
            """INSERT OR IGNORE INTO mirror_transition_recovery
               SELECT transition_key,frozen_hash,thread_id,'pending',0,NULL,NULL,NULL,NULL,
                      prepared_at,prepared_at,NULL
               FROM mirror_binding_transitions WHERE state!='starter_verified'"""
        )
        conn.execute(
            "INSERT INTO mirror_schema_version(singleton,version) VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET version=excluded.version",
            (MIRROR_SCHEMA_VERSION,),
        )
        validate_mirror_schema(conn)
        conn.commit()
        return MIRROR_SCHEMA_VERSION
    except Exception:
        conn.rollback()
        raise


def validate_mirror_schema(conn: sqlite3.Connection) -> int:
    """Fail with an actionable error when required version/capabilities differ."""
    row = conn.execute(
        "SELECT version FROM mirror_schema_version WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise MirrorSchemaError("mirror schema is uninitialized; call initialize_mirror_schema() before starting workers")
    version = int(row[0])
    if version != MIRROR_SCHEMA_VERSION:
        raise MirrorSchemaError(
            f"mirror schema version {version} is unsupported (expected {MIRROR_SCHEMA_VERSION}); run schema initialization with this Hermes release"
        )
    required = {
        "mirror_initiatives", "mirror_members", "mirror_notes", "mirror_inbox_receipts",
        "mirror_reaction_states", "mirror_conversation_events", "mirror_discord_thread_cursors",
        "mirror_discord_inbound_state", "mirror_conversation_deliveries",
        "mirror_conversation_delivery_items", "mirror_binding_epochs",
        "mirror_binding_transitions", "mirror_discord_outbox", "mirror_transition_recovery",
        "mirror_reconciliation_findings", "mirror_thread_quarantine", "mirror_terminal_lifecycles",
    }
    present = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(required - present)
    if missing:
        raise MirrorSchemaError("mirror schema is missing required tables: " + ", ".join(missing))
    return version
