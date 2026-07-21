from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from plugins.platforms.discord.kanban_mirror.conversation_log import record_conversation_event
from plugins.platforms.discord.kanban_mirror.state import (
    active_thread_binding,
    add_member,
    backfill_legacy_bindings,
    connect_mirror,
    create_initiative,
    resolve_thread_task,
    set_thread,
)


def _legacy(conn, *, initiative="init-1", thread="thread-1", task="task-1"):
    create_initiative(conn, initiative, "Fixture")
    add_member(conn, initiative, task)
    set_thread(conn, initiative, thread, "starter-1")


def test_backfill_is_idempotent_and_resolver_compatible(tmp_path):
    path = tmp_path / "fixture-board" / "mirror.db"
    conn = connect_mirror(path)
    _legacy(conn)
    assert resolve_thread_task(path, "forum-unused", "thread-1") == ("task-1", "fixture-board")
    assert backfill_legacy_bindings(conn, "fixture-board") == 1
    assert backfill_legacy_bindings(conn, "fixture-board") == 0
    binding = active_thread_binding(conn, "thread-1")
    assert binding is not None
    created = conn.execute("SELECT created_at FROM mirror_initiatives").fetchone()[0]
    assert (binding.task_id, binding.sequence, binding.started_at) == ("task-1", 1, created)
    assert resolve_thread_task(path, "forum-unused", "thread-1") == ("task-1", "fixture-board")
    conn.close()


def test_backfill_skips_ambiguous_mapping_and_preserves_unbound_event(tmp_path):
    conn = connect_mirror(tmp_path / "mirror.db")
    _legacy(conn)
    add_member(conn, "init-1", "task-2")
    assert backfill_legacy_bindings(conn, "board") == 0
    event = record_conversation_event(conn, discord_message_id="m1", thread_id="thread-1", binding_key=None, event_class="conversation.human", author_label="User", content="keep me")
    assert event.binding_key is None
    assert conn.execute("SELECT content FROM mirror_conversation_events").fetchone()[0] == "keep me"
    conn.close()


def test_event_keeps_epoch_active_when_created(tmp_path):
    conn = connect_mirror(tmp_path / "mirror.db")
    _legacy(conn)
    backfill_legacy_bindings(conn, "board")
    event = record_conversation_event(conn, discord_message_id="m1", thread_id="thread-1", binding_key=None, event_class="conversation.human", author_label="User", content="epoch one")
    conn.execute("UPDATE mirror_binding_epochs SET state='closed',ended_at=200 WHERE binding_key=?", (event.binding_key,))
    conn.execute("INSERT INTO mirror_binding_epochs VALUES (?,?,?,?,?,?,?,?,?,?)", ("binding:thread-1:2", "thread-1", "board", "task-2", 2, 201, None, None, None, "open"))
    conn.commit()
    assert conn.execute("SELECT binding_key FROM mirror_conversation_events WHERE id=?", (event.id,)).fetchone()[0] == "binding:thread-1:1"
    conn.close()


def test_constraints_and_concurrent_backfill(tmp_path):
    path = tmp_path / "mirror.db"
    conn = connect_mirror(path)
    _legacy(conn)
    conn.close()

    def run():
        worker = connect_mirror(path)
        try:
            return backfill_legacy_bindings(worker, "board")
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: run(), range(2))) == [0, 1]
    conn = connect_mirror(path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO mirror_binding_epochs VALUES (?,?,?,?,?,?,?,?,?,?)", ("other", "thread-1", "board", "task-2", 2, 2, None, None, None, "open"))
    conn.close()
