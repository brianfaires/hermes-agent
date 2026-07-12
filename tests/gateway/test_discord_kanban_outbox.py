from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gateway.platforms.base import Platform
from gateway.run import GatewayRunner
from gateway.kanban_mirror.outbox import OutboundEnvelope, deliver, enqueue, get
from gateway.kanban_mirror.state import connect_mirror


@pytest.fixture
def conn(tmp_path):
    db = connect_mirror(tmp_path / "mirror.db")
    yield db
    db.close()


def envelope(content="exact response"):
    return OutboundEnvelope(
        profile="reviewer",
        thread_id="2002",
        reply_to_message_id="4004",
        content=content,
        attachments=("artifact.txt",),
        correlation_id="discord:stable-turn",
    )


def test_enqueue_freezes_exact_payload_and_is_idempotent(conn):
    operation_id = enqueue(conn, envelope())
    assert enqueue(conn, envelope()) == operation_id
    row = get(conn, operation_id)
    payload = json.loads(row["payload"])
    assert payload == {
        "attachments": ["artifact.txt"],
        "binding_key": None,
        "content": "exact response",
        "correlation_id": "discord:stable-turn",
        "profile": "reviewer",
        "reply_to_message_id": "4004",
        "thread_id": "2002",
    }
    assert row["target_profile"] == "reviewer"
    assert row["status"] == "pending"
    assert conn.execute("SELECT count(*) FROM mirror_discord_outbox").fetchone()[0] == 1


def test_same_stable_operation_refuses_changed_retry_payload(conn):
    enqueue(conn, envelope())
    with pytest.raises(ValueError, match="different frozen payload"):
        enqueue(conn, envelope("regenerated response"))
    assert json.loads(conn.execute("SELECT payload FROM mirror_discord_outbox").fetchone()[0])["content"] == "exact response"


@pytest.mark.asyncio
async def test_missing_or_disconnected_profile_fails_closed_and_stays_retryable(conn):
    operation_id = enqueue(conn, envelope())
    called = False

    async def sender(_adapter, _payload):
        nonlocal called
        called = True

    assert await deliver(conn, operation_id, None, send=sender) is False
    assert await deliver(conn, operation_id, SimpleNamespace(is_connected=False), send=sender) is False
    row = get(conn, operation_id)
    assert called is False
    assert row["status"] == "pending"
    assert row["attempt_count"] == 2
    assert "disconnected" in row["last_error"]


@pytest.mark.asyncio
async def test_retry_uses_frozen_payload_and_marks_success_only_when_confirmed(conn):
    operation_id = enqueue(conn, envelope())
    seen = []

    async def unconfirmed(_adapter, payload):
        seen.append(payload)
        return SimpleNamespace(success=True, message_id=None)

    adapter = SimpleNamespace(is_connected=True)
    assert await deliver(conn, operation_id, adapter, send=unconfirmed) is False
    assert get(conn, operation_id)["status"] == "confirmation_needed"
    # Operators may explicitly resolve uncertainty before a manual retry.
    conn.execute("UPDATE mirror_discord_outbox SET status='pending',confirmation_needed_at=NULL WHERE operation_id=?", (operation_id,))
    conn.commit()

    async def confirmed(_adapter, payload):
        seen.append(payload)
        return SimpleNamespace(success=True, message_id="9009")

    assert await deliver(conn, operation_id, adapter, send=confirmed) is True
    row = get(conn, operation_id)
    assert row["status"] == "delivered"
    assert row["discord_message_id"] == "9009"
    assert row["attempt_count"] == 2
    assert seen[0] == seen[1]
    assert seen[1]["content"] == "exact response"

    # A duplicate worker observes the receipt and never sends again.
    assert await deliver(conn, operation_id, adapter, send=unconfirmed) is True
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_live_claim_prevents_concurrent_duplicate_send(conn):
    operation_id = enqueue(conn, envelope())
    conn.execute(
        "UPDATE mirror_discord_outbox SET status='sending',updated_at=strftime('%s','now') WHERE operation_id=?",
        (operation_id,),
    )
    conn.commit()
    called = False

    async def sender(_adapter, _payload):
        nonlocal called
        called = True

    assert await deliver(
        conn, operation_id, SimpleNamespace(is_connected=True), send=sender
    ) is False
    assert called is False


@pytest.mark.asyncio
async def test_executable_mirrored_response_uses_profile_outbox_and_agent_ledger(tmp_path, monkeypatch):
    db_path = tmp_path / "live-mirror.db"
    from gateway.kanban_mirror import context as context_mod, state as state_mod
    monkeypatch.setattr(context_mod, "resolve_mirrored_kanban_thread", lambda _: SimpleNamespace(board_slug="fixture", initiative_kind="single"))
    monkeypatch.setattr(state_mod, "mirror_db_path", lambda _: db_path)
    monkeypatch.setattr(state_mod, "active_thread_binding", lambda *_: SimpleNamespace(binding_key="binding-at-creation"))
    sent = []

    class ProfileAdapter:
        is_connected = True
        async def send(self, chat_id, content, **kwargs):
            sent.append((chat_id, content, kwargs))
            return SimpleNamespace(success=True, message_id="confirmed-agent-77")

    adapter = ProfileAdapter()
    runner = SimpleNamespace(_discord_adapter_for_profile=lambda profile: adapter if profile == "reviewer" else None)
    source = SimpleNamespace(platform=Platform.DISCORD, thread_id="thread-9", chat_id="thread-9")
    event = SimpleNamespace(outbound_profile="reviewer", correlation_id="discord:turn-9", message_id="human-9", media_urls=[], route_marker="discord-kanban-conversation")
    assert await GatewayRunner._deliver_mirrored_kanban_response(runner, event=event, source=source, content="frozen final text")
    assert sent[0][0:2] == ("thread-9", "frozen final text")
    db = connect_mirror(db_path)
    try:
        out = db.execute("SELECT * FROM mirror_discord_outbox").fetchone()
        ledger = db.execute("SELECT * FROM mirror_conversation_events WHERE discord_message_id='confirmed-agent-77'").fetchone()
        assert out["status"] == "delivered" and out["target_profile"] == "reviewer"
        assert ledger["event_class"] == "conversation.agent"
        assert ledger["binding_key"] == "binding-at-creation"
        assert ledger["author_label"] == "reviewer"
    finally:
        db.close()


def test_only_stable_mirrored_marker_selects_outbox_surface():
    source = SimpleNamespace(platform=Platform.DISCORD, thread_id="thread-x")
    routed = SimpleNamespace(outbound_profile="ops", correlation_id="discord:x", route_marker="discord-kanban-conversation")
    ordinary = SimpleNamespace(outbound_profile=None, correlation_id=None, route_marker=None)
    assert GatewayRunner._is_mirrored_kanban_conversation_event(routed, source)
    directive = SimpleNamespace(
        outbound_profile="ops", correlation_id="discord:directive-x",
        route_marker="discord-kanban-directive",
    )
    assert GatewayRunner._is_mirrored_kanban_conversation_event(directive, source)
    assert not GatewayRunner._is_mirrored_kanban_conversation_event(ordinary, source)
    assert not GatewayRunner._is_mirrored_kanban_conversation_event(routed, SimpleNamespace(platform=Platform.TELEGRAM, thread_id="thread-x"))
