"""Native subscription wakes use an explicit route without relaxing the guard."""
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from agent.inference_policy import inference_scope
from gateway.run import GatewayRunner
from hermes_cli import runtime_provider


MODEL = "gpt-6.1-sol"
PROVIDER = "openai-codex"
CODEX_URL = "https://chatgpt.com/backend-api/codex"


def test_native_runtime_resolves_explicit_subscription_route(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = {"model": {"provider": PROVIDER, "default": MODEL},
           "fallback_providers": [{"provider": "openrouter", "model": "paid"}]}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    credentials = Mock(return_value={"api_key": "fixture-only", "base_url": CODEX_URL})
    monkeypatch.setattr(runtime_provider, "resolve_codex_runtime_credentials", credentials)
    monkeypatch.setattr(runtime_provider, "load_pool", lambda provider: None)
    runner = object.__new__(GatewayRunner)
    with inference_scope(True):
        model, runtime = runner._resolve_session_agent_runtime(user_config=cfg)
    assert model == MODEL
    assert runtime["provider"] == runtime["requested_provider"] == PROVIDER
    assert runtime["api_mode"] == "codex_responses"
    assert runtime["base_url"] == CODEX_URL
    credentials.assert_called_once_with()


def test_subscription_route_uses_session_then_channel_before_global(tmp_path, monkeypatch):
    from gateway.config import ChannelOverride, GatewayConfig, Platform, PlatformConfig
    from gateway.session import SessionSource

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = {"model": {"provider": "openrouter", "default": "paid"}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    credentials = Mock(return_value={"api_key": "fixture-only", "base_url": CODEX_URL})
    monkeypatch.setattr(runtime_provider, "resolve_codex_runtime_credentials", credentials)
    monkeypatch.setattr(runtime_provider, "load_pool", lambda provider: None)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(
        enabled=True, channel_overrides={"chat": ChannelOverride(provider=PROVIDER, model="channel-model")},
    )})
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", profile="default")
    with inference_scope(True):
        model, runtime = runner._resolve_session_agent_runtime(source=source, session_key="scope", user_config=cfg)
        assert model == "channel-model" and runtime["provider"] == PROVIDER
        runner._session_state("scope").conversation.model_override = {
            "provider": PROVIDER, "model": MODEL, "base_url": CODEX_URL,
        }
        model, runtime = runner._resolve_session_agent_runtime(source=source, session_key="scope", user_config=cfg)
        assert model == MODEL and runtime["provider"] == PROVIDER
    assert credentials.call_count == 2


def test_paid_session_override_is_denied_before_credential_io(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = {"model": {"provider": PROVIDER, "default": MODEL}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    credentials = Mock(side_effect=AssertionError("credential I/O must not be reached"))
    monkeypatch.setattr(runtime_provider, "resolve_codex_runtime_credentials", credentials)
    monkeypatch.setattr(runtime_provider, "resolve_requested_provider", credentials)
    runner = object.__new__(GatewayRunner)
    runner._session_state("scope").conversation.model_override = {
        "provider": "openrouter", "model": "paid", "api_key": "fixture-only",
    }
    with inference_scope(True), pytest.raises(RuntimeError, match="subscription_only"):
        runner._resolve_session_agent_runtime(session_key="scope", user_config=cfg)
    credentials.assert_not_called()
