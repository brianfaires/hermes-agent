from __future__ import annotations

import sqlite3

import pytest

from plugins.platforms.discord.kanban_mirror.conversation_log import (
    freeze_log_delivery,
    mark_log_delivery,
    parse_log_command,
    record_conversation_event,
    render_log_comment,
    resolve_log_targets,
    select_log_events,
    recover_log_deliveries,
    split_log_comment,
)
from plugins.platforms.discord.kanban_mirror.state import connect_mirror


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


def test_binding_epoch_targets_keep_lifecycle_history_on_its_own_cards(mirror_conn):
    mirror_conn.execute(
        """INSERT INTO mirror_binding_epochs
           (binding_key,thread_id,board_slug,task_id,sequence,started_at,ended_at,state)
           VALUES ('epoch-old','thread-1','default','card-old',1,1,2,'closed'),
                  ('epoch-current','thread-1','default','card-current',2,2,NULL,'open')"""
    )
    old = add_event(mirror_conn, "old", binding="epoch-old", content="old discussion")
    current = add_event(
        mirror_conn, "current", binding="epoch-current", content="current discussion"
    )

    current_targets = resolve_log_targets(
        mirror_conn, command=log_command("!log"), thread_id="thread-1"
    )
    assert [(target.binding_key, target.task_id) for target in current_targets] == [
        ("epoch-current", "card-current")
    ]
    all_targets = resolve_log_targets(
        mirror_conn, command=log_command("!log all"), thread_id="thread-1"
    )
    assert [(target.binding_key, target.task_id) for target in all_targets] == [
        ("epoch-old", "card-old"), ("epoch-current", "card-current")
    ]

    deliveries = [
        freeze_log_delivery(
            mirror_conn, operation_id=f"all-{target.binding_key}",
            trigger_discord_message_id="command", thread_id="thread-1",
            task_id=target.task_id, command=log_command("!log all"),
            binding_key=target.binding_key, scope_all_to_binding=True,
        )
        for target in all_targets
    ]
    assert deliveries[0] is not None and deliveries[0].event_ids == (old.id,)
    assert deliveries[1] is not None and deliveries[1].event_ids == (current.id,)
    assert "current discussion" not in deliveries[0].payload
    assert "old discussion" not in deliveries[1].payload


def test_epoch_log_fails_closed_without_one_active_binding(mirror_conn):
    mirror_conn.execute(
        """INSERT INTO mirror_binding_epochs
           (binding_key,thread_id,board_slug,task_id,sequence,started_at,ended_at,state)
           VALUES ('epoch-old','thread-1','default','card-old',1,1,2,'closed')"""
    )
    event = add_event(mirror_conn, "old", binding="epoch-old")
    with pytest.raises(ValueError, match="exactly one active binding"):
        resolve_log_targets(
            mirror_conn,
            command=log_command("!log", replied_to_message_id="old"),
            thread_id="thread-1",
        )
    assert mirror_conn.execute(
        "SELECT COUNT(*) FROM mirror_conversation_delivery_items WHERE event_id=?", (event.id,)
    ).fetchone()[0] == 0


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
        "mirror_conversation_delivery_chunks",
    } <= tables


def test_rich_event_and_recoverable_frozen_chunk(mirror_conn):
    event = record_conversation_event(
        mirror_conn, discord_message_id="rich", thread_id="thread-1",
        binding_key="task-current", event_class="directive.agent_disposition",
        author_label="reviewer", author_id="42", content="accepted",
        replied_to_message_id="source", reply_context="please review",
        discord_created_at=123, discord_message_link="https://discord.test/rich",
        binding_task_id="card-7", binding_interval="100..open",
        attachments=({"filename": "proof.txt", "url": "https://cdn/proof"},),
        artifacts=({"name": "report", "sha256": "abc"},),
    )
    rendered = render_log_comment([event])
    assert "reviewer (Discord user 42):" in rendered
    assert "timestamp: 123" in rendered and "reply to source — please review" in rendered
    assert "card card-7; interval 100..open" in rendered
    assert "proof.txt" in rendered and "sha256=abc" in rendered
    assert split_log_comment(rendered, limit=128) == split_log_comment(rendered, limit=128)

    frozen = freeze_log_delivery(
        mirror_conn, operation_id="chunk-op", trigger_discord_message_id="cmd",
        thread_id="thread-1", task_id="card-7", command=log_command("!log"),
        binding_key="task-current",
    )
    assert frozen is not None
    uncertain = recover_log_deliveries(
        mirror_conn, worker_id="w1", now=100, write_comment=lambda *_args: None
    )
    assert uncertain == {"claimed": 1, "delivered": 0, "failed": 1}
    row = mirror_conn.execute(
        "SELECT status,next_attempt_at FROM mirror_conversation_delivery_chunks WHERE operation_id='chunk-op'"
    ).fetchone()
    assert row["status"] == "failed" and row["next_attempt_at"] > 100
    recovered = recover_log_deliveries(
        mirror_conn, worker_id="w2", now=row["next_attempt_at"],
        write_comment=lambda *_args: 77,
    )
    assert recovered == {"claimed": 1, "delivered": 1, "failed": 0}
    assert mirror_conn.execute(
        "SELECT status FROM mirror_conversation_deliveries WHERE operation_id='chunk-op'"
    ).fetchone()[0] == "delivered"


def test_chunking_is_utf8_bounded_and_rehydrates_pre_chunk_delivery(mirror_conn):
    add_event(mirror_conn, "large", content=("é" * 10_000) + "\n\nend")
    command = log_command("!log")
    frozen = freeze_log_delivery(
        mirror_conn, operation_id="old-op", trigger_discord_message_id="cmd",
        thread_id="thread-1", task_id="task-current", command=command,
        binding_key="task-current",
    )
    assert frozen is not None
    chunks = split_log_comment(frozen.payload, limit=128)
    assert chunks and all(len(chunk.encode("utf-8")) <= 128 for chunk in chunks)

    # Simulate an additive-upgrade database whose delivery predates chunks.
    mirror_conn.execute(
        "DELETE FROM mirror_conversation_delivery_chunks WHERE operation_id='old-op'"
    )
    mirror_conn.commit()
    replay = freeze_log_delivery(
        mirror_conn, operation_id="old-op", trigger_discord_message_id="cmd",
        thread_id="thread-1", task_id="task-current", command=command,
        binding_key="task-current",
    )
    assert replay == frozen
    stored = mirror_conn.execute(
        "SELECT payload FROM mirror_conversation_delivery_chunks "
        "WHERE operation_id='old-op' ORDER BY chunk_index"
    ).fetchall()
    assert "".join(row[0] for row in stored) == frozen.payload
