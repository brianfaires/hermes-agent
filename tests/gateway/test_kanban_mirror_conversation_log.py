from __future__ import annotations

import sqlite3

import pytest

from gateway.kanban_mirror.conversation_log import (
    freeze_log_delivery,
    mark_log_delivery,
    parse_log_command,
    record_conversation_event,
    render_log_comment,
    select_log_events,
)
from gateway.kanban_mirror.state import connect_mirror


@pytest.fixture
def mirror_conn(tmp_path):
    conn = connect_mirror(tmp_path / "mirror.db")
    try:
        yield conn
    finally:
        conn.close()


def log_command(text: str, *, replied_to_message_id: str | None = None):
    command = parse_log_command(text, replied_to_message_id=replied_to_message_id)
    assert command is not None
    return command


def add_event(
    conn,
    message_id: str,
    *,
    binding: str = "task-current",
    event_class: str = "conversation.human",
    author: str = "Brian",
    content: str | None = None,
):
    return record_conversation_event(
        conn,
        discord_message_id=message_id,
        thread_id="thread-1",
        binding_key=binding,
        event_class=event_class,
        author_label=author,
        content=content or f"message {message_id}",
        discord_created_at=100,
    )


@pytest.mark.parametrize(
    ("text", "reply_id", "mode", "note"),
    [
        ("!log", None, "current", ""),
        (" !LOG decision note ", None, "current", "decision note"),
        ("!log", "m1", "reply", ""),
        ("!log use this", "m1", "reply", "use this"),
        ("!log all", None, "all", ""),
        ("!log ALL", "m1", "all", ""),
        ("!log all details", None, "current", "all details"),
    ],
)
def test_parse_log_command(text, reply_id, mode, note):
    command = parse_log_command(text, replied_to_message_id=reply_id)
    assert command is not None
    assert command.mode == mode
    assert command.note == note


@pytest.mark.parametrize("text", ["", "discussion", "!logger", "!pause", "please !log"])
def test_unknown_text_is_not_a_log_command(text):
    assert parse_log_command(text) is None


def test_event_replay_preserves_original_immutable_content(mirror_conn):
    first = add_event(mirror_conn, "m1", content="original")
    replay = add_event(mirror_conn, "m1", binding="different", content="changed replay")

    assert replay == first
    assert replay.content == "original"
    assert replay.binding_key == "task-current"
    count = mirror_conn.execute("SELECT COUNT(*) FROM mirror_conversation_events").fetchone()[0]
    assert count == 1


def test_current_and_all_selection_include_agents_but_exclude_mirror_noise(mirror_conn):
    old = add_event(mirror_conn, "m-old", binding="task-old")
    human = add_event(mirror_conn, "m-human")
    agent = add_event(
        mirror_conn,
        "m-agent",
        event_class="conversation.agent",
        author="Ops",
        content="implemented the fixture",
    )
    add_event(mirror_conn, "m-ack", event_class="mirror.ack", author="Kanban", content="logged")

    current = select_log_events(
        mirror_conn,
        command=log_command("!log") ,
        thread_id="thread-1",
        binding_key="task-current",
    )
    assert [event.id for event in current] == [human.id, agent.id]

    all_events = select_log_events(
        mirror_conn,
        command=log_command("!log all"),
        thread_id="thread-1",
        binding_key="task-current",
    )
    assert [event.id for event in all_events] == [old.id, human.id, agent.id]


def test_reply_log_can_intentionally_repeat_a_delivered_message(mirror_conn):
    event = add_event(mirror_conn, "m1")
    command = log_command("!log useful context", replied_to_message_id="m1")
    delivery = freeze_log_delivery(
        mirror_conn,
        operation_id="op-1",
        trigger_discord_message_id="cmd-1",
        thread_id="thread-1",
        task_id="task-current",
        command=command,
        binding_key="task-current",
    )
    assert delivery is not None
    mark_log_delivery(mirror_conn, operation_id="op-1", status="delivered", kanban_comment_id=17)

    repeat = freeze_log_delivery(
        mirror_conn,
        operation_id="op-2",
        trigger_discord_message_id="cmd-2",
        thread_id="thread-1",
        task_id="task-current",
        command=command,
        binding_key="task-current",
    )
    assert repeat is not None
    assert repeat.event_ids == (event.id,)


def test_batch_delivery_freezes_payload_and_excludes_only_successful_items(mirror_conn):
    first = add_event(mirror_conn, "m1", content="first")
    command = log_command("!log Decision: proceed")
    frozen = freeze_log_delivery(
        mirror_conn,
        operation_id="op-1",
        trigger_discord_message_id="cmd-1",
        thread_id="thread-1",
        task_id="task-current",
        command=command,
        binding_key="task-current",
    )
    assert frozen is not None
    assert frozen.event_ids == (first.id,)
    assert "Decision: proceed" in frozen.payload

    second = add_event(mirror_conn, "m2", content="arrived later")
    retried = freeze_log_delivery(
        mirror_conn,
        operation_id="op-1",
        trigger_discord_message_id="cmd-1",
        thread_id="thread-1",
        task_id="task-current",
        command=command,
        binding_key="task-current",
    )
    assert retried == frozen
    assert "arrived later" not in retried.payload

    mark_log_delivery(mirror_conn, operation_id="op-1", status="failed", error="temporary")
    still_pending = select_log_events(
        mirror_conn,
        command=log_command("!log"),
        thread_id="thread-1",
        binding_key="task-current",
    )
    assert [event.id for event in still_pending] == [second.id]

    delivered = mark_log_delivery(
        mirror_conn,
        operation_id="op-1",
        status="delivered",
        kanban_comment_id=99,
    )
    assert delivered.status == "delivered"
    assert delivered.attempt_count == 2

    next_batch = select_log_events(
        mirror_conn,
        command=log_command("!log"),
        thread_id="thread-1",
        binding_key="task-current",
    )
    assert [event.id for event in next_batch] == [second.id]


def test_pending_delivery_reserves_batch_events_from_other_operations(mirror_conn):
    first = add_event(mirror_conn, "m1")
    command = log_command("!log")
    frozen = freeze_log_delivery(
        mirror_conn,
        operation_id="op-1",
        trigger_discord_message_id="cmd-1",
        thread_id="thread-1",
        task_id="task-current",
        command=command,
        binding_key="task-current",
    )
    assert frozen is not None and frozen.event_ids == (first.id,)

    overlapping = freeze_log_delivery(
        mirror_conn,
        operation_id="op-2",
        trigger_discord_message_id="cmd-2",
        thread_id="thread-1",
        task_id="task-current",
        command=command,
        binding_key="task-current",
    )
    assert overlapping is None


def test_operation_id_replay_rejects_conflicting_identity(mirror_conn):
    add_event(mirror_conn, "m1")
    command = log_command("!log")
    assert freeze_log_delivery(
        mirror_conn,
        operation_id="same-op",
        trigger_discord_message_id="cmd-1",
        thread_id="thread-1",
        task_id="task-current",
        command=command,
        binding_key="task-current",
    ) is not None

    with pytest.raises(ValueError, match="different log request"):
        freeze_log_delivery(
            mirror_conn,
            operation_id="same-op",
            trigger_discord_message_id="cmd-other",
            thread_id="thread-other",
            task_id="task-other",
            command=command,
            binding_key="task-current",
        )


def test_batch_selection_uses_discord_chronology_not_ingestion_order(mirror_conn):
    later = record_conversation_event(
        mirror_conn,
        discord_message_id="m-later",
        thread_id="thread-1",
        binding_key="task-current",
        event_class="conversation.human",
        author_label="Brian",
        content="later",
        discord_created_at=200,
    )
    earlier = record_conversation_event(
        mirror_conn,
        discord_message_id="m-earlier",
        thread_id="thread-1",
        binding_key="task-current",
        event_class="conversation.agent",
        author_label="Ops",
        content="earlier",
        discord_created_at=100,
    )
    selected = select_log_events(
        mirror_conn,
        command=log_command("!log"),
        thread_id="thread-1",
        binding_key="task-current",
    )
    assert [event.id for event in selected] == [earlier.id, later.id]


def test_delivery_replay_is_byte_stable_across_connections(tmp_path):
    path = tmp_path / "mirror.db"
    first_conn = connect_mirror(path)
    second_conn = connect_mirror(path)
    try:
        add_event(first_conn, "m1", content="line one\nline two")
        command = log_command("!log note")
        first = freeze_log_delivery(
            first_conn,
            operation_id="same-op",
            trigger_discord_message_id="cmd-1",
            thread_id="thread-1",
            task_id="task-current",
            command=command,
            binding_key="task-current",
        )
        replay = freeze_log_delivery(
            second_conn,
            operation_id="same-op",
            trigger_discord_message_id="cmd-1",
            thread_id="thread-1",
            task_id="task-current",
            command=command,
            binding_key="task-current",
        )
        assert replay == first
        assert replay is not None
        assert replay.payload_hash
    finally:
        first_conn.close()
        second_conn.close()


def test_render_preserves_multiline_content_and_source_ids(mirror_conn):
    human = add_event(mirror_conn, "m1", content="first line\nsecond line")
    agent = add_event(
        mirror_conn,
        "m2",
        event_class="conversation.agent",
        author="Reviewer",
        content="review complete",
    )
    rendered = render_log_comment([human, agent], note="Use this outcome.")

    assert "Brian:\nfirst line\nsecond line" in rendered
    assert "Reviewer:\nreview complete" in rendered
    assert "Log note:\nUse this outcome." in rendered
    assert rendered.endswith("Source messages: m1, m2")


def test_current_log_requires_unambiguous_binding(mirror_conn):
    add_event(mirror_conn, "m1")
    with pytest.raises(ValueError, match="unambiguous binding_key"):
        freeze_log_delivery(
            mirror_conn,
            operation_id="op",
            trigger_discord_message_id="cmd",
            thread_id="thread-1",
            task_id="task-current",
            command=log_command("!log"),
            binding_key=None,
        )


def test_schema_addition_does_not_change_existing_mirror_tables(mirror_conn):
    tables = {
        row[0]
        for row in mirror_conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "mirror_initiatives",
        "mirror_members",
        "mirror_inbox_receipts",
        "mirror_conversation_events",
        "mirror_conversation_deliveries",
        "mirror_conversation_delivery_items",
    } <= tables
