import asyncio
from types import SimpleNamespace

import pytest

from plugins.platforms.discord.kanban_mirror.outbox import OutboundEnvelope, enqueue, get
from plugins.platforms.discord.kanban_mirror.state import connect_mirror
from plugins.platforms.discord.kanban_mirror.supervision import LoopSupervisor
from gateway.platforms.base import Platform
from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_live_router_recovery_exact_profile_duplicate_start_and_health(tmp_path, monkeypatch):
    path = tmp_path / "mirror.db"
    conn = connect_mirror(path)
    operation_id = enqueue(conn, OutboundEnvelope(
        profile="reviewer", thread_id="thread-1", reply_to_message_id="human-1",
        content="durable answer", attachments=(), correlation_id="corr-1",
        binding_key=None,
    ))
    conn.close()

    sent = []
    adapter = SimpleNamespace(_running=True, is_connected=True)

    async def send(chat_id, content, **kwargs):
        sent.append((chat_id, content, kwargs))
        return SimpleNamespace(success=True, message_id="agent-1")

    adapter.send = send
    ingress = SimpleNamespace(_running=True, is_connected=True)
    cfg = SimpleNamespace(enabled=True, conversation_router_enabled=True, board_slug="board")
    statuses = []
    monkeypatch.setattr("plugins.platforms.discord.kanban_mirror.inbox.load_config", lambda: cfg)
    monkeypatch.setattr("plugins.platforms.discord.kanban_mirror.state.mirror_db_path", lambda slug: path)
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: statuses.append(kw))

    runner = SimpleNamespace(
        _gateway_profile_name="default", adapters={Platform.DISCORD: ingress},
        _profile_adapters={"reviewer": {Platform.DISCORD: adapter}}, _running=True,
        _kanban_mirror_supervisor=LoopSupervisor(base_backoff=.001, max_backoff=.01),
        _kanban_router_board_slug=None,
    )
    runner._discord_adapter_for_profile = GatewayRunner._discord_adapter_for_profile.__get__(runner)
    runner._kanban_profile_adapters = GatewayRunner._kanban_profile_adapters.__get__(runner)
    start = GatewayRunner._start_kanban_router_runtime.__get__(runner)
    start(interval=.01, health_interval=.01)
    first_tasks = dict(runner._kanban_mirror_supervisor._tasks)
    start(interval=.01, health_interval=.01)  # Discord ready/reconnect is idempotent.
    assert runner._kanban_mirror_supervisor._tasks == first_tasks

    for _ in range(100):
        check = connect_mirror(path)
        delivered = get(check, operation_id)["status"] == "delivered"
        ledger = check.execute(
            "SELECT COUNT(*) FROM mirror_conversation_events WHERE discord_message_id='agent-1' AND event_class='conversation.agent'"
        ).fetchone()[0]
        check.close()
        if delivered:
            break
        await asyncio.sleep(.01)

    assert sent == [("thread-1", "durable answer", {
        "reply_to": "human-1", "metadata": {"thread_id": "thread-1", "suppress_embeds": True}
    })]
    assert ledger == 1
    assert any(item.get("kanban_mirror", {}).get("router_enabled") for item in statuses)
    runner._running = False
    await runner._kanban_mirror_supervisor.stop()
    assert all(state["state"] == "stopped" for state in runner._kanban_mirror_supervisor.snapshot().values())
    assert statuses[-1] == {"kanban_mirror": {}}


@pytest.mark.asyncio
async def test_router_runtime_disabled_clears_stale_health(monkeypatch):
    cfg = SimpleNamespace(enabled=True, conversation_router_enabled=False, board_slug="board")
    statuses = []
    monkeypatch.setattr("plugins.platforms.discord.kanban_mirror.inbox.load_config", lambda: cfg)
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: statuses.append(kw))
    runner = SimpleNamespace(
        _gateway_profile_name="default", adapters={}, _profile_adapters={},
        _kanban_mirror_supervisor=LoopSupervisor(), _kanban_router_board_slug=None,
    )
    runner._discord_adapter_for_profile = GatewayRunner._discord_adapter_for_profile.__get__(runner)
    GatewayRunner._start_kanban_router_runtime(runner)
    assert statuses == [{"kanban_mirror": {}}]
    assert runner._kanban_mirror_supervisor._tasks == {}
