"""Regression coverage for profile-safe Telegram clarify and pending routing."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import build_session_key
from plugins.platforms.telegram.adapter import TelegramAdapter
from tools import clarify_gateway


def _telegram(token: str) -> TelegramAdapter:
    return TelegramAdapter(PlatformConfig(enabled=True, token=token))


def _event(adapter: TelegramAdapter, text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=adapter.build_source(
            chat_id="8244556262",
            chat_type="dm",
            user_id="8244556262",
        ),
    )


@pytest.mark.asyncio
async def test_secondary_telegram_clarify_and_pending_events_stay_in_own_profile():
    """Same Telegram user may talk to two bots without crossing profiles."""
    default = _telegram("default-bot-token")
    ops = _telegram("ops-bot-token")
    ops._inbound_profile = "ops"

    default_seen = []
    ops_seen = []
    default.set_message_handler(lambda event: default_seen.append(event))

    async def _ops_handler(event):
        ops_seen.append(event)
        key = build_session_key(event.source, profile=event.source.profile)
        assert clarify_gateway.resolve_text_response_for_session(key, event.text)
        return ""

    ops.set_message_handler(_ops_handler)
    default._send_with_retry = AsyncMock()
    ops._send_with_retry = AsyncMock()

    initial = _event(ops, "first answer")
    assert initial.source.profile == "ops"
    ops_key = build_session_key(initial.source, profile=initial.source.profile)
    default_key = build_session_key(initial.source, profile=None)
    assert ops_key.startswith("agent:ops:telegram:dm:")
    assert default_key.startswith("agent:main:telegram:dm:")
    ops._active_sessions[ops_key] = asyncio.Event()
    clarify_gateway.register("ops-clarify", ops_key, "Question?", None)

    try:
        # First message is routed into the Ops clarify waiter, never queued as
        # a default-profile follow-up.
        await ops.handle_message(initial)
        assert [event.source.profile for event in ops_seen] == ["ops"]
        assert not default_seen
        assert default_key not in ops._pending_messages
        assert not default._pending_messages
        default._send_with_retry.assert_not_awaited()

        # Once clarify has resumed, a second text while the Ops turn remains
        # active is held in the Ops adapter's pending slot for cascade.
        clarify_gateway.clear_session(ops_key)
        followup = _event(ops, "second answer")
        await ops.handle_message(followup)
        queued = ops.get_pending_message(ops_key)
        assert queued is followup
        assert queued.source.profile == "ops"
        assert default_key not in ops._pending_messages
        assert not default._pending_messages
        assert not default_seen
        default._send_with_retry.assert_not_awaited()
    finally:
        clarify_gateway.clear_session(ops_key)


@pytest.mark.asyncio
async def test_secondary_telegram_rejects_conflicting_source_profile():
    """Credential-owned adapters fail closed instead of falling back to default."""
    ops = _telegram("ops-bot-token")
    ops._inbound_profile = "ops"
    ops.gateway_runner = SimpleNamespace(
        _profile_name_for_source=lambda source: "default",
    )
    seen = []
    ops.set_message_handler(lambda event: seen.append(event))

    event = _event(ops, "wrong profile")
    assert event.source.profile == "default"
    await ops.handle_message(event)

    assert not seen
    assert not ops._active_sessions
    assert not ops._pending_messages