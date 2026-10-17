"""Passive ingress evidence; all messages and files here are offline fixtures."""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.gateway.test_discord_connect import _ensure_discord_mock

_ensure_discord_mock()

import plugins.platforms.discord.adapter as module
from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


def adapter(tmp_path, monkeypatch, profile="ops", bot_id=111):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / profile))
    obj = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    obj.set_owner_profile(profile)
    obj._client = SimpleNamespace(user=SimpleNamespace(id=bot_id))
    obj._ready_event.set()
    obj._handle_message = AsyncMock(return_value=True)
    return obj


def message(obj, message_id=222):
    return SimpleNamespace(
        id=message_id, author=obj._client.user,
        channel=SimpleNamespace(id=333), content="PRIVATE_BODY_SENTINEL",
        attachments=[SimpleNamespace(url="PRIVATE_URL_SENTINEL")],
    )


def observations(caplog):
    return [json.loads(record.getMessage().removeprefix("discord_ingress "))
            for record in caplog.records
            if record.getMessage().startswith("discord_ingress ")]


@pytest.mark.asyncio
async def test_live_rejection_records_only_owner_and_message_metadata(tmp_path, monkeypatch, caplog):
    obj = adapter(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO, logger=module.__name__)
    assert await obj._dispatch_discord_message(message(obj)) is False
    rows = observations(caplog)
    assert [r["stage"] for r in rows] == ["received", "rejected"]
    assert rows[-1]["reason"] == "self_message"
    for row in rows:
        assert row["profile"] == "ops"
        assert row["home"] == str(tmp_path / "ops")
        assert row["bot_id"] == "111"
        assert row["message_id"] == "222"
        assert row["channel_id"] == "333"
        assert row["thread_id"] is None
        assert row["parent_id"] is None
        assert row["received_count"] == 1
    assert rows[1]["seq"] == rows[0]["seq"] + 1
    assert rows[1]["generation"] == rows[0]["generation"]
    assert "PRIVATE" not in json.dumps(rows)
    obj._handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["duplicate", "message_type", "bots_disabled", "bot_mention_required",
                                  "bot_inline_mention_required", "user_not_allowed", "other_bot_mentioned",
                                  "mention_required", "admitted"])
async def test_admission_reasons(tmp_path, monkeypatch, caplog, case):
    obj = adapter(tmp_path, monkeypatch)
    msg = message(obj)
    msg.author = SimpleNamespace(id=444, bot=False)
    msg.type = module.discord.MessageType.default
    msg.mentions = []
    msg.guild = SimpleNamespace(id=555)
    monkeypatch.setattr(obj, "_is_allowed_user", lambda *a, **k: case != "user_not_allowed")
    monkeypatch.setattr(obj, "_self_is_explicitly_mentioned", lambda m: case == "bot_inline_mention_required")
    monkeypatch.setattr(obj, "_self_is_raw_mentioned", lambda m: False)
    monkeypatch.setattr(obj, "_discord_bots_require_inline_mention", lambda: True)
    monkeypatch.setattr(obj, "_get_allow_bots", lambda: "none" if case == "bots_disabled" else "mentions")
    monkeypatch.setattr(obj, "_discord_free_response_channels", lambda: set())
    monkeypatch.setenv("DISCORD_IGNORE_NO_MENTION", "true")
    if case == "duplicate":
        obj._dedup.is_duplicate(str(msg.id))
    if case == "message_type":
        msg.type = "unsupported"
    if case.startswith("bot_") or case == "bots_disabled":
        msg.author.bot = True
    if case in {"other_bot_mentioned", "mention_required"}:
        msg.mentions = [SimpleNamespace(id=777, bot=case == "other_bot_mentioned")]
    caplog.set_level(logging.INFO, logger=module.__name__)
    result = await obj._dispatch_discord_message(msg)
    assert result is (case == "admitted")
    rows = observations(caplog)
    assert rows[-1]["stage"] == ("admitted" if result else "rejected")
    assert rows[-1]["reason"] == ("adapter_policy" if result else case)


@pytest.mark.asyncio
async def test_liveness_emits_quiet_progress_and_stale_transport_reason(tmp_path, monkeypatch, caplog):
    obj = adapter(tmp_path, monkeypatch)
    obj._running = True
    obj._liveness_failure_threshold = 99
    samples = iter([(True, "healthy"), (False, "ack_stale")])
    monkeypatch.setattr(obj, "_read_websocket_health", lambda client: next(samples))
    calls = 0

    async def tick(_):
        nonlocal calls
        calls += 1
        if calls == 3:
            obj._running = False

    monkeypatch.setattr(module.asyncio, "sleep", tick)
    caplog.set_level(logging.INFO, logger=module.__name__)
    await obj._liveness_loop()
    rows = observations(caplog)
    assert [r["reason"] for r in rows] == ["healthy", "ack_stale"]
    assert all(r["stage"] == "progress" and r["received_count"] == 0 for r in rows)
    assert rows[1]["seq"] == rows[0]["seq"] + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["channel_not_allowed", "channel_ignored", "mention_required",
                                  "auto_thread_failed", "mention_only", "handoff", "batched"])
async def test_real_message_handler_observations(tmp_path, monkeypatch, caplog, case):
    from datetime import datetime, timezone

    obj = adapter(tmp_path, monkeypatch)
    del obj._handle_message
    msg = message(obj)
    msg.author = SimpleNamespace(id=444, bot=False, name="PRIVATE_NAME", display_name="PRIVATE_NAME")
    msg.type = module.discord.MessageType.default
    msg.guild = SimpleNamespace(id=555)
    msg.mentions = []
    msg.attachments = []
    msg.reference = None
    msg.created_at = datetime.now(timezone.utc)
    msg.channel.send = AsyncMock()
    if case == "mention_only":
        msg.content = "<@111>"
    monkeypatch.setattr(obj, "_is_allowed_user", lambda *a, **k: True)
    monkeypatch.setattr(obj, "_self_is_explicitly_mentioned", lambda m: case == "mention_only")
    monkeypatch.setattr(obj, "_get_allowed_channels", lambda: {"999"} if case == "channel_not_allowed" else set())
    monkeypatch.setattr(obj, "_get_ignored_channels", lambda: {"333"} if case == "channel_ignored" else set())
    monkeypatch.setattr(obj, "_discord_require_mention", lambda: case == "mention_required")
    monkeypatch.setattr(obj, "_discord_history_backfill", lambda: False)
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true" if case == "auto_thread_failed" else "false")
    obj._auto_create_thread = AsyncMock(return_value=None)
    obj.handle_message = AsyncMock()
    obj._text_batch_delay_seconds = 1 if case == "batched" else 0
    enqueued = []
    monkeypatch.setattr(obj, "_enqueue_text_event", enqueued.append)
    caplog.set_level(logging.INFO, logger=module.__name__)
    accepted = case in {"handoff", "batched"}
    assert await obj._dispatch_discord_message(msg) is accepted
    rows = observations(caplog)
    assert rows[-1]["stage"] == ("dispatched" if accepted else "rejected")
    assert rows[-1]["reason"] == case
    assert "PRIVATE" not in json.dumps(rows)
    assert bool(enqueued) is (case == "batched")


def test_primary_profile_is_captured_at_construction(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "primary"))
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "primary")
    obj = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    obj._client = SimpleNamespace(user=SimpleNamespace(id=111))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "foreign"))
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "foreign")
    caplog.set_level(logging.INFO, logger=module.__name__)
    obj._observe_ingress("progress", "healthy")
    row = observations(caplog)[-1]
    assert row["profile"] == "primary"
    assert row["home"] == str(tmp_path / "primary")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("PRIVATE_EXCEPTION"), asyncio.CancelledError()])
async def test_errors_preserve_dispatch_exception_without_retaining_text(tmp_path, monkeypatch, caplog, error):
    obj = adapter(tmp_path, monkeypatch)
    obj._handle_message.side_effect = error
    monkeypatch.setattr(obj, "_discord_message_admission", lambda *a, **k: (True, False))
    caplog.set_level(logging.INFO, logger=module.__name__)
    with pytest.raises(type(error)):
        await obj._dispatch_discord_message(message(obj))
    rows = observations(caplog)
    assert rows[-1]["stage"] == "error"
    assert rows[-1]["reason"] == ("cancelled" if isinstance(error, asyncio.CancelledError) else "dispatch_error")
    assert "PRIVATE" not in json.dumps(rows)


@pytest.mark.asyncio
async def test_same_channel_two_profiles_and_thread_ids(tmp_path, monkeypatch, caplog):
    ops = adapter(tmp_path, monkeypatch, "ops", 111)
    other = adapter(tmp_path, monkeypatch, "other", 112)
    caplog.set_level(logging.INFO, logger=module.__name__)
    thread = object.__new__(module.discord.Thread)
    thread.id, thread.parent_id = 333, 334
    for obj in (ops, other):
        msg = message(obj)
        msg.channel = thread
        await obj._dispatch_discord_message(msg)
    rows = observations(caplog)
    selected = [r for r in rows if (r["profile"], r["home"], r["bot_id"]) ==
                ("ops", str(tmp_path / "ops"), "111")]
    assert len(selected) == 2
    assert all(r["thread_id"] == "333" and r["parent_id"] == "334" for r in selected)
    assert rows[0]["generation"] != rows[-1]["generation"]
    assert selected[-1]["received_count"] == 1


def test_existing_queue_rotation_retains_sequence_and_exposes_missing_records(tmp_path, monkeypatch, caplog):
    import hermes_logging as hl
    from agent.redact import RedactingFormatter

    obj = adapter(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO, logger=module.__name__)
    hl._reset_queued_handlers()
    log = tmp_path / "shared" / "gateway.log"
    try:
        hl._add_rotating_handler(logging.getLogger(), log, level=logging.INFO,
                                 max_bytes=1400, backup_count=3,
                                 formatter=RedactingFormatter(hl._LOG_FORMAT),
                                 log_filter=hl._ComponentFilter(hl.COMPONENT_PREFIXES["gateway"]))
        for _ in range(3):
            obj._observe_ingress("progress", "healthy")
        # Simulate disabled/dropped logging without changing listener state.
        with monkeypatch.context() as m:
            m.setattr(module.logger, "info", lambda *a, **k: None)
            obj._observe_ingress("received", "on_message", message(obj))
        obj._observe_ingress("progress", "healthy")
        hl.flush_log_queue()
        files = list(log.parent.glob("gateway.log*"))
        assert len(files) > 1
        rows = []
        for path in files:
            for line in path.read_text().splitlines():
                marker = f" {module.__name__}: discord_ingress "
                if marker in line:
                    rows.append(json.loads(line.split(marker, 1)[1]))
        rows.sort(key=lambda r: r["seq"])
        assert [r["seq"] for r in rows] == [1, 2, 3, 5]
        assert [r["received_count"] for r in rows] == [0, 0, 0, 1]
        assert len({r["generation"] for r in rows}) == 1
        # Fresh contiguous progress with unchanged count proves observed quiet;
        # a hole, truncated rotation window or stale tail does not.
        assert rows[1]["seq"] == rows[0]["seq"] + 1
        assert rows[1]["received_count"] == rows[0]["received_count"]
        assert rows[-1]["seq"] != rows[-2]["seq"] + 1
        now = rows[-1]["timestamp"] + 100
        assert now - rows[-1]["timestamp"] > 60
        assert all(r["stage"] == "progress" for r in rows)
    finally:
        hl._reset_queued_handlers()


def test_observer_failure_and_unbound_adapter_do_not_borrow_home(tmp_path, monkeypatch, caplog):
    obj = adapter(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO, logger=module.__name__)
    obj._observe_ingress("progress", "healthy")
    with monkeypatch.context() as m:
        def broken(*a, **k):
            raise RuntimeError("PRIVATE_EXCEPTION")
        m.setattr(module.logger, "info", broken)
        obj._observe_ingress("progress", "healthy")
    obj._observe_ingress("progress", "healthy")
    del obj._runtime_profile_home
    obj._observe_ingress("progress", "healthy")
    rows = observations(caplog)
    assert [r["seq"] for r in rows] == [1, 3]
    assert "PRIVATE" not in json.dumps(rows)


@pytest.mark.asyncio
async def test_recovery_is_not_live_ingress_evidence(tmp_path, monkeypatch, caplog):
    obj = adapter(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO, logger=module.__name__)
    assert obj._discord_message_admission(message(obj), claim=False) == (False, False)
    del obj._handle_message
    msg = message(obj, 223)
    msg.author = SimpleNamespace(id=444, bot=False)
    msg.type = module.discord.MessageType.default
    msg.mentions = []
    monkeypatch.setattr(obj, "_self_is_explicitly_mentioned", lambda m: False)
    monkeypatch.setattr(obj, "_get_allowed_channels", lambda: {"999"})
    assert await obj._handle_message(msg, recovered=True) is False
    assert observations(caplog) == []
