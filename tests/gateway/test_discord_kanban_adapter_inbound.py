from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from gateway.kanban_mirror.backfill import DiscordBackfillIngestor, DiscordInbound, HistoryPage
from gateway.kanban_mirror.state import connect_mirror
from plugins.platforms.discord import adapter as discord_adapter


def _bind(conn, thread_id: str) -> None:
    conn.execute(
        """INSERT INTO mirror_binding_epochs
        (binding_key,thread_id,board_slug,task_id,sequence,started_at,state)
        VALUES (?,?,?,?,1,1,'open')""",
        (f"binding-{thread_id}", thread_id, "fixture", f"card-{thread_id}"),
    )
    conn.commit()


def _message(mid: int, thread: str = "10", forum: str = "99", *, bot: bool = False):
    return SimpleNamespace(
        id=mid,
        channel=SimpleNamespace(id=int(thread), parent_id=int(forum)),
        author=SimpleNamespace(id=7, bot=bot, display_name="Fixture Human", name="fixture"),
        content=f"message {mid}",
        reference=None,
        created_at=datetime.now(timezone.utc),
        type=discord_adapter.discord.MessageType.default,
    )


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    conn = connect_mirror(tmp_path / "mirror.db")
    ingestor = DiscordBackfillIngestor(conn, clock=lambda: 1_000)
    cfg = SimpleNamespace(
        enabled=True,
        conversation_router_enabled=True,
        forum_channel_ids=frozenset({"99"}),
    )
    adapter = discord_adapter.DiscordAdapter(PlatformConfig(enabled=True, token="fixture"))
    adapter._kanban_mirror_conn = conn

    async def configured_runtime():
        return cfg, ingestor

    monkeypatch.setattr(adapter, "_kanban_runtime", configured_runtime)
    yield adapter, conn, ingestor
    conn.close()


@pytest.mark.asyncio
async def test_real_adapter_live_observes_before_dispatch_and_unrelated_is_unchanged(runtime):
    adapter, conn, _ingestor = runtime
    _bind(conn, "10")
    adapter._kanban_backfilled_threads.add("10")

    observed, relevant = await adapter._observe_kanban_message(_message(101))
    assert (observed, relevant) == (True, True)
    row = conn.execute(
        "SELECT classification,processing_status FROM mirror_discord_inbound_state WHERE discord_message_id='101'"
    ).fetchone()
    assert tuple(row) == ("pending", "pending")

    observed, relevant = await adapter._observe_kanban_message(_message(102, forum="123"))
    assert (observed, relevant) == (False, False)
    assert conn.execute(
        "SELECT 1 FROM mirror_discord_inbound_state WHERE discord_message_id='102'"
    ).fetchone() is None


@pytest.mark.asyncio
async def test_real_adapter_unmapped_and_bot_noise_are_durable(runtime):
    adapter, conn, _ingestor = runtime
    adapter._kanban_backfilled_threads.add("55")
    await adapter._observe_kanban_message(_message(201, thread="55"))
    await adapter._observe_kanban_message(_message(202, thread="55", bot=True))
    rows = conn.execute(
        "SELECT classification,processing_status FROM mirror_discord_inbound_state ORDER BY discord_message_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("unmapped", "pending"), ("noise", "processed")]


@pytest.mark.asyncio
async def test_real_adapter_reconnect_backfill_retries_fetch_failure(runtime, monkeypatch):
    adapter, conn, _ingestor = runtime
    _bind(conn, "10")
    attempts = 0

    async def fetch(thread_id, after, limit):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("Discord history unavailable")
        return HistoryPage([DiscordInbound("301", thread_id, "older", created_at=999)])

    monkeypatch.setattr(adapter, "_fetch_kanban_history_page", fetch)
    await adapter._backfill_kanban_mirror_threads()
    assert "10" not in adapter._kanban_backfilled_threads
    await adapter._backfill_kanban_mirror_threads()
    assert "10" in adapter._kanban_backfilled_threads
    assert attempts == 2
    assert conn.execute(
        "SELECT processing_status FROM mirror_discord_inbound_state WHERE discord_message_id='301'"
    ).fetchone()[0] == "pending"


@pytest.mark.asyncio
async def test_real_adapter_live_backfill_overlap_uses_stable_message_id(runtime, monkeypatch):
    adapter, conn, _ingestor = runtime
    _bind(conn, "10")

    async def fetch(thread_id, after, limit):
        return HistoryPage([
            DiscordInbound("401", thread_id, "same message", created_at=999),
            DiscordInbound("400", thread_id, "older", created_at=998),
        ])

    monkeypatch.setattr(adapter, "_fetch_kanban_history_page", fetch)
    await adapter._observe_kanban_message(_message(401))
    ids = conn.execute(
        "SELECT discord_message_id FROM mirror_conversation_events ORDER BY CAST(discord_message_id AS INT)"
    ).fetchall()
    assert [row[0] for row in ids] == ["400", "401"]
    assert conn.execute(
        "SELECT COUNT(*) FROM mirror_discord_inbound_state WHERE discord_message_id='401'"
    ).fetchone()[0] == 1
