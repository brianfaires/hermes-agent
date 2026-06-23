"""Wiring test: _handle_message routes engineering work to the engteam handoff
when the front desk is enabled, and otherwise falls through to the agent."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _clear_auth_env(monkeypatch) -> None:
    for key in (
        "TELEGRAM_ALLOWED_USERS", "WHATSAPP_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS",
        "TELEGRAM_ALLOW_ALL_USERS", "WHATSAPP_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_id="m1",
        source=SessionSource(
            platform=Platform.WHATSAPP,
            user_id="15551234567@s.whatsapp.net",
            chat_id="15551234567@s.whatsapp.net",
            user_name="tester",
            chat_type="dm",
        ),
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)})
    runner.adapters = {Platform.WHATSAPP: SimpleNamespace(send=AsyncMock())}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_store._is_rate_limited.return_value = False
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._update_prompt_pending = {}
    return runner


def _allow_all(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda name, **kw: [])


@pytest.mark.asyncio
async def test_engineering_message_is_handed_off_when_front_desk_enabled(monkeypatch):
    _allow_all(monkeypatch)
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"agent": {"front_desk": {"enabled": True}}})
    monkeypatch.setattr("gateway.engteam_handoff.handoff_engineering",
                        lambda message, **kw: "Engineering picked it up (ack).")

    agent_called = {"count": 0}

    async def _capture(event, source, _quick_key, _run_generation):
        agent_called["count"] += 1
        return "agent ran"

    runner = _make_runner()
    runner._handle_message_with_agent = _capture  # noqa: SLF001

    result = await runner._handle_message(_make_event("please debug the auth code"))

    assert result == "Engineering picked it up (ack)."
    assert agent_called["count"] == 0  # never spun up an agent


@pytest.mark.asyncio
async def test_engineering_message_falls_through_when_front_desk_disabled(monkeypatch):
    _allow_all(monkeypatch)
    # Front desk disabled -> plan returns normal_agent -> no handoff.
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})

    def _boom(*a, **k):  # handoff must NOT be called
        raise AssertionError("handoff should not run when front desk is disabled")

    monkeypatch.setattr("gateway.engteam_handoff.handoff_engineering", _boom)

    async def _capture(event, source, _quick_key, _run_generation):
        return "agent ran"

    runner = _make_runner()
    runner._handle_message_with_agent = _capture  # noqa: SLF001

    result = await runner._handle_message(_make_event("please debug the auth code"))

    assert result == "agent ran"
