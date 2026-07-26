"""Transport-profile isolation for unauthorized-DM pairing flow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, SessionSource
from gateway.run import GatewayRunner


def _adapter(dm_policy: str):
    return SimpleNamespace(
        _dm_policy=dm_policy,
        _running=True,
        send=AsyncMock(),
    )


def _store(*, code=None):
    store = MagicMock()
    store._is_rate_limited.return_value = False
    store.generate_code.return_value = code
    return store


@pytest.fixture
def runner(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    instance = GatewayRunner(GatewayConfig())
    instance._is_user_authorized = lambda _source: False
    return instance


@pytest.mark.asyncio
async def test_routed_runtime_cannot_enable_pairing_on_disabled_transport(runner):
    primary = _adapter("disabled")
    routed = _adapter("pairing")
    global_store = _store(code="GLOBAL")
    routed_store = _store(code="ROUTED")
    runner.adapters = {Platform.WECOM: primary}
    runner._profile_adapters = {"routed": {Platform.WECOM: routed}}
    runner.pairing_store = global_store
    runner.pairing_stores = {"routed": routed_store}

    source = SessionSource(
        platform=Platform.WECOM,
        user_id="unknown",
        chat_id="dm-chat",
        chat_type="dm",
        profile="routed",
    )
    source._transport_adapter_ref = lambda: primary

    await runner._handle_message(MessageEvent(text="hello", source=source))

    global_store.generate_code.assert_not_called()
    routed_store.generate_code.assert_not_called()
    primary.send.assert_not_awaited()
    routed.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_secondary_transport_generates_code_in_its_own_pairing_store(runner):
    primary = _adapter("disabled")
    secondary = _adapter("pairing")
    global_store = _store(code="GLOBAL")
    secondary_store = _store(code="CODER")
    runner.adapters = {Platform.WECOM: primary}
    runner._profile_adapters = {"coder": {Platform.WECOM: secondary}}
    runner.pairing_store = global_store
    runner.pairing_stores = {"coder": secondary_store}

    source = SessionSource(
        platform=Platform.WECOM,
        user_id="unknown",
        chat_id="dm-chat",
        chat_type="dm",
        profile="coder",
    )
    source._transport_adapter_ref = lambda: secondary

    await runner._handle_message(MessageEvent(text="hello", source=source))

    secondary_store._is_rate_limited.assert_called_once_with("wecom", "unknown")
    secondary_store.generate_code.assert_called_once_with("wecom", "unknown", "")
    global_store.generate_code.assert_not_called()
    secondary.send.assert_awaited_once()
    primary.send.assert_not_awaited()
