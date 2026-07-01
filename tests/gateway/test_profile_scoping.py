import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.platforms.base import Platform
from gateway.session import SessionSource
from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_single_profile_gateway_pins_prompt_home_across_env_drift(monkeypatch, tmp_path):
    root = tmp_path / "hermes"
    default_home = root
    ops_home = root / "profiles" / "ops"
    default_home.mkdir(parents=True)
    ops_home.mkdir(parents=True)
    (default_home / "SOUL.md").write_text("DEFAULT PERSONA\n", encoding="utf-8")
    (ops_home / "SOUL.md").write_text("OPS PERSONA\n", encoding="utf-8")
    (default_home / "config.yaml").write_text(
        "model:\n  default: default-model\n", encoding="utf-8"
    )
    (ops_home / "config.yaml").write_text(
        "model:\n  default: ops-model\n", encoding="utf-8"
    )

    monkeypatch.setenv("HERMES_HOME", str(ops_home))

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._gateway_profile_home = ops_home.resolve()
    runner._gateway_profile_name = "ops"

    observed = {}

    async def fake_inner(self, *args, **kwargs):
        # Simulate stale process/env drift after the gateway was started.  The
        # gateway turn must still build prompt/profile state from the captured
        # Ops profile home, not from the current process env/default home.
        os.environ["HERMES_HOME"] = str(default_home)

        def check_in_worker_thread():
            from agent.prompt_builder import load_soul_md
            from gateway.run import _load_gateway_config
            from hermes_cli.profiles import get_active_profile_name
            from hermes_constants import get_hermes_home

            return {
                "home": str(get_hermes_home()),
                "profile": get_active_profile_name(),
                "soul": load_soul_md(),
                "model": _load_gateway_config()["model"]["default"],
            }

        observed.update(
            await GatewayRunner._run_in_executor_with_context(self, check_in_worker_thread)
        )
        return {"final_response": "ok"}

    monkeypatch.setattr(GatewayRunner, "_run_agent_inner", fake_inner)

    source = SessionSource(platform=Platform.DISCORD, chat_id="c1", chat_type="dm")
    result = await runner._run_agent(
        "hello",
        "",
        [],
        source,
        "session-1",
        session_key="agent:main:discord:dm:c1",
    )

    assert result["final_response"] == "ok"
    assert observed == {
        "home": str(ops_home.resolve()),
        "profile": "ops",
        "soul": "OPS PERSONA",
        "model": "ops-model",
    }


def test_agent_cache_signature_changes_when_profile_home_changes():
    base = {
        "model": "test-model",
        "runtime": {"api_key": "key", "base_url": "https://example.test", "provider": "custom"},
        "enabled_toolsets": ["terminal"],
        "ephemeral_prompt": "",
        "cache_keys": {},
        "user_id": "u1",
        "user_id_alt": None,
    }

    sig_default = GatewayRunner._agent_config_signature(
        **base,
        profile_home="/tmp/hermes",
    )
    sig_ops = GatewayRunner._agent_config_signature(
        **base,
        profile_home="/tmp/hermes/profiles/ops",
    )

    assert sig_default != sig_ops


def test_cron_provider_start_is_pinned_to_gateway_profile_home(monkeypatch, tmp_path):
    from gateway.run import _start_profile_scoped_cron_provider
    from hermes_constants import get_hermes_home
    from cron import scheduler as cron_scheduler

    default_home = tmp_path / "hermes"
    ops_home = default_home / "profiles" / "ops"
    default_home.mkdir(parents=True)
    ops_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    observed = {}

    class FakeCronProvider:
        def start(self, stop_event, *, adapters=None, loop=None):
            observed["home"] = str(get_hermes_home())
            observed["scheduler_home"] = str(cron_scheduler._get_hermes_home())
            observed["adapters"] = adapters
            observed["loop"] = loop

    _start_profile_scoped_cron_provider(
        FakeCronProvider(),
        SimpleNamespace(),
        profile_home=ops_home,
        adapters={"telegram": object()},
        loop="loop-token",
    )

    assert observed["home"] == str(ops_home.resolve())
    assert observed["scheduler_home"] == str(ops_home.resolve())
    assert "telegram" in observed["adapters"]
    assert observed["loop"] == "loop-token"
    assert cron_scheduler._hermes_home is None
