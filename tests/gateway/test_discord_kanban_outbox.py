from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

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
    assert get(conn, operation_id)["status"] == "pending"

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
