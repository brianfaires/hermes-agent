from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.kanban_mirror.backfill import DiscordBackfillIngestor, DiscordInbound, HistoryPage
from plugins.platforms.discord.kanban_mirror.state import connect_mirror
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


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed_users,allowed_roles,roles,expected,reason", [
    ({"7"}, set(), [], True, "allowed_user"),
    (set(), {42}, [42], True, "allowed_role"),
    ({"8"}, {42}, [41], False, "user_and_role_not_allowed"),
])
async def test_mirrored_ingress_freezes_exact_user_role_authorization(
    runtime, allowed_users, allowed_roles, roles, expected, reason,
):
    adapter, conn, _ingestor = runtime
    _bind(conn, "10")
    adapter._kanban_backfilled_threads.add("10")
    adapter._allowed_user_ids = allowed_users
    adapter._allowed_role_ids = allowed_roles
    guild = SimpleNamespace(id=99, get_member=lambda _uid: None)
    message = _message(501)
    message.guild = guild
    message.author.guild = guild
    message.author.roles = [SimpleNamespace(id=value) for value in roles]
    await adapter._observe_kanban_message(message)
    payload = json.loads(conn.execute(
        "SELECT payload FROM mirror_discord_inbound_state WHERE discord_message_id='501'"
    ).fetchone()[0])
    assert payload["authorized"] is expected
    assert payload["authorization_reason"] == reason
    assert payload["authorization_policy"]["ingress_adapter"] == adapter.name


@pytest.mark.asyncio
async def test_restart_replay_keeps_frozen_unauthorized_disposition(runtime, monkeypatch):
    from plugins.platforms.discord.kanban_mirror.inbound import PendingInboundRunner

    adapter, conn, _ingestor = runtime
    _bind(conn, "10")
    adapter._kanban_backfilled_threads.add("10")
    adapter._allowed_user_ids = {"8"}
    await adapter._observe_kanban_message(_message(601))
    dispatched = False

    async def forbidden_dispatch(_event):
        nonlocal dispatched
        dispatched = True

    monkeypatch.setattr(adapter, "_dispatch_message_event", forbidden_dispatch)
    adapter._allowed_user_ids = {"7"}  # changed after simulated restart
    runner = PendingInboundRunner(conn, adapter._process_kanban_pending_inbound, clock=lambda: 2_000)
    assert await runner.run_once() == 1
    row = conn.execute(
        "SELECT processing_status FROM mirror_discord_inbound_state WHERE discord_message_id='601'"
    ).fetchone()
    disposition = conn.execute(
        "SELECT disposition FROM mirror_discord_inbound_dispositions WHERE discord_message_id='601'"
    ).fetchone()
    assert row[0] == "processed"
    assert disposition[0] == "unauthorized"
    assert dispatched is False


@pytest.mark.asyncio
async def test_only_validated_ingress_backfills_and_freezes_its_policy(tmp_path, monkeypatch):
    from plugins.platforms.discord.kanban_mirror.inbound import PendingInboundRunner

    conn = connect_mirror(tmp_path / "mirror.db")
    _bind(conn, "10")
    ingestor = DiscordBackfillIngestor(conn, clock=lambda: 1_000)
    cfg = SimpleNamespace(enabled=True, conversation_router_enabled=True,
                          forum_channel_ids=frozenset({"99"}))
    monkeypatch.setattr("plugins.platforms.discord.kanban_mirror.inbox.load_config", lambda: cfg)

    def make(bot_id, allowed):
        item = discord_adapter.DiscordAdapter(PlatformConfig(enabled=True, token="fixture"))
        item._client = SimpleNamespace(user=SimpleNamespace(id=bot_id))
        item._running = True
        item._disconnecting = False
        item._ready_event.set()
        item._kanban_mirror_conn = conn
        item._kanban_ingestor = ingestor
        item._allowed_user_ids = allowed
        return item

    non_ingress = make("111", {"7"})
    ingress = make("222", {"8"})
    non_ingress._kanban_router_ingress_identity = None
    ingress._kanban_router_ingress_identity = ("owner", "222")
    ingress._kanban_router_profile = "owner"
    fetches = {"non_ingress": 0, "ingress": 0}

    async def non_ingress_fetch(*_args):
        fetches["non_ingress"] += 1
        return HistoryPage([])

    async def ingress_fetch(thread_id, _after, _limit):
        fetches["ingress"] += 1
        return HistoryPage([ingress._discord_inbound(_message(701, thread=thread_id), relevant=True)])

    monkeypatch.setattr(non_ingress, "_fetch_kanban_history_page", non_ingress_fetch)
    monkeypatch.setattr(ingress, "_fetch_kanban_history_page", ingress_fetch)
    await non_ingress._backfill_kanban_mirror_threads()
    assert fetches == {"non_ingress": 0, "ingress": 0}
    await ingress._backfill_kanban_mirror_threads()
    assert fetches == {"non_ingress": 0, "ingress": 1}
    payload = json.loads(conn.execute(
        "SELECT payload FROM mirror_discord_inbound_state WHERE discord_message_id='701'"
    ).fetchone()[0])
    assert payload["authorized"] is False
    assert payload["authorization_policy"]["ingress_bot_id"] == "222"

    ingress._allowed_user_ids = {"7"}
    dispatched = False

    async def dispatch(_event):
        nonlocal dispatched
        dispatched = True

    monkeypatch.setattr(ingress, "_dispatch_message_event", dispatch)
    assert await PendingInboundRunner(conn, ingress._process_kanban_pending_inbound,
                                      clock=lambda: 2_000).run_once() == 1
    assert not dispatched

    ingress._client.user.id = "111"
    await ingress._backfill_kanban_mirror_threads()
    assert ingress._kanban_router_ingress_identity is None
    conn.close()


@pytest.mark.asyncio
async def test_validated_ingress_worker_start_is_idempotent():
    adapter = discord_adapter.DiscordAdapter(PlatformConfig(enabled=True, token="fixture"))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id="222"))
    adapter._running = True
    adapter._disconnecting = False
    adapter._ready_event.set()
    adapter._kanban_router_profile = "owner"
    adapter._kanban_router_ingress_identity = ("owner", "222")

    adapter.start_kanban_ingress_workers()
    inbound = adapter._kanban_inbound_task
    backfill = adapter._kanban_backfill_task
    adapter.start_kanban_ingress_workers()

    assert adapter._kanban_inbound_task is inbound
    assert adapter._kanban_backfill_task is backfill
    assert set(adapter._kanban_supervisor._tasks) == {"pending-inbound", "reconnect-backfill"}
    await adapter._kanban_supervisor.stop()
