from types import SimpleNamespace

import pytest
import yaml

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.model_switch import ModelSwitchResult


@pytest.mark.asyncio
async def test_model_command_persists_to_gateway_profile_home_not_process_home(tmp_path, monkeypatch):
    default_home = tmp_path / "default-home"
    ops_home = tmp_path / "profiles" / "ops"
    default_home.mkdir(parents=True)
    ops_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    (default_home / "config.yaml").write_text(
        yaml.safe_dump({"model": {"default": "default-old", "provider": "openai-codex"}}),
        encoding="utf-8",
    )
    (ops_home / "config.yaml").write_text(
        yaml.safe_dump({"model": {"default": "ops-old", "provider": "openai-codex"}}),
        encoding="utf-8",
    )

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(
        multiplex_profiles=False,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    runner._gateway_profile_home = ops_home.resolve()
    runner._gateway_profile_name = "ops"
    runner.adapters = {}
    runner.session_store = None
    runner._session_db = None
    runner._session_model_overrides = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None

    monkeypatch.setattr(
        "hermes_cli.model_switch.switch_model",
        lambda **kwargs: ModelSwitchResult(
            success=True,
            new_model="gpt-5.5",
            target_provider="openai-codex",
            provider_label="OpenAI Codex",
            api_mode="codex_responses",
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.model_cost_guard.expensive_model_warning",
        lambda *args, **kwargs: None,
    )

    event = MessageEvent(
        text="/model gpt-5.5 --global",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="ops-room",
            chat_type="channel",
            user_id="user-1",
            profile="ops",
        ),
    )

    reply = await GatewayRunner._handle_model_command(runner, event)

    assert "gpt-5.5" in reply
    default_cfg = yaml.safe_load((default_home / "config.yaml").read_text(encoding="utf-8"))
    ops_cfg = yaml.safe_load((ops_home / "config.yaml").read_text(encoding="utf-8"))
    assert default_cfg["model"]["default"] == "default-old"
    assert ops_cfg["model"]["default"] == "gpt-5.5"
    assert ops_cfg["model"]["provider"] == "openai-codex"
