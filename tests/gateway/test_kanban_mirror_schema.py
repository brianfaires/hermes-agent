from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway.kanban_mirror.schema import (
    MIRROR_SCHEMA_VERSION,
    MirrorSchemaError,
    initialize_mirror_schema,
    validate_mirror_schema,
)


def _connection(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def test_initialization_migrates_representative_legacy_schema_without_data_loss() -> None:
    conn = _connection()
    conn.executescript(
        """
        CREATE TABLE mirror_initiatives(
          id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        INSERT INTO mirror_initiatives VALUES('legacy','Keep me',123);
        CREATE TABLE mirror_discord_outbox(
          operation_id TEXT PRIMARY KEY, correlation_id TEXT NOT NULL,
          target_profile TEXT NOT NULL, thread_id TEXT NOT NULL,
          reply_to_message_id TEXT, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0,
          last_error TEXT, discord_message_id TEXT, created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL, delivered_at INTEGER
        );
        """
    )

    assert initialize_mirror_schema(conn) == MIRROR_SCHEMA_VERSION
    row = conn.execute("SELECT id,title,created_at FROM mirror_initiatives").fetchone()
    assert tuple(row) == ("legacy", "Keep me", 123)
    initiative_columns = {r[1] for r in conn.execute("PRAGMA table_info(mirror_initiatives)")}
    outbox_columns = {r[1] for r in conn.execute("PRAGMA table_info(mirror_discord_outbox)")}
    assert {"kind", "blocked_reasons", "archived_at", "updated_at"} <= initiative_columns
    assert {"next_attempt_at", "lease_owner", "confirmation_needed_at", "quarantined_at"} <= outbox_columns
    assert validate_mirror_schema(conn) == MIRROR_SCHEMA_VERSION
    assert initialize_mirror_schema(conn) == MIRROR_SCHEMA_VERSION


def test_concurrent_initializers_serialize(tmp_path) -> None:
    path = str(tmp_path / "mirror.db")

    def initialize() -> int:
        conn = _connection(path)
        try:
            return initialize_mirror_schema(conn)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: initialize(), range(16))) == [MIRROR_SCHEMA_VERSION] * 16
    conn = _connection(path)
    assert validate_mirror_schema(conn) == MIRROR_SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM mirror_schema_version").fetchone()[0] == 1


def test_interrupted_initialization_rolls_back_all_ddl() -> None:
    conn = _connection()

    def fail(step: str) -> None:
        if step == "columns":
            raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError, match="injected"):
        initialize_mirror_schema(conn, _after_step=fail)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'mirror_%'"
    ).fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        validate_mirror_schema(conn)
    assert initialize_mirror_schema(conn) == MIRROR_SCHEMA_VERSION


def test_future_schema_fails_with_actionable_error() -> None:
    conn = _connection()
    initialize_mirror_schema(conn)
    conn.execute("UPDATE mirror_schema_version SET version=999")
    conn.commit()
    with pytest.raises(MirrorSchemaError, match="newer.*upgrade Hermes"):
        initialize_mirror_schema(conn)


def test_initialized_database_opens_every_persistence_capability() -> None:
    conn = _connection()
    initialize_mirror_schema(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {
        "mirror_initiatives", "mirror_members", "mirror_notes", "mirror_inbox_receipts",
        "mirror_reaction_states", "mirror_conversation_events", "mirror_discord_thread_cursors",
        "mirror_discord_inbound_state", "mirror_conversation_deliveries",
        "mirror_conversation_delivery_items", "mirror_binding_epochs",
        "mirror_binding_transitions", "mirror_discord_outbox", "mirror_transition_recovery",
        "mirror_reconciliation_findings", "mirror_thread_quarantine", "mirror_terminal_lifecycles",
    }
    assert expected <= tables
