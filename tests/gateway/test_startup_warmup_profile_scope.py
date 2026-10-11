"""Boot turn-machinery warm-up must resolve credentials like a real turn (#99373).

The warm-up runs ``model_tools.get_tool_definitions`` on an executor thread
before any inbound turn exists. Under ``gateway.multiplex_profiles`` an
unscoped ``get_secret`` fails closed (``UnscopedSecretError``), so every
credential-gated ``check_fn`` raised during warm-up and the probe cache it is
meant to prime was never filled. The warm-up must run inside the default
profile's runtime scope, the same seam ``load_gateway_config_for_runner``
uses for primary startup.
"""

import pytest

import model_tools
from agent import secret_scope
from gateway import run as gateway_run
from hermes_constants import get_hermes_home
from tools.discord_tool import check_discord_tool_requirements


@pytest.fixture
def warmup_probe(monkeypatch):
    """Replace tool materialization with the real Discord credential gate."""
    seen = []

    def fake_get_tool_definitions(**_kwargs):
        seen.append(check_discord_tool_requirements())
        return [{"name": "probe"}]

    monkeypatch.setattr(model_tools, "get_tool_definitions", fake_get_tool_definitions)
    monkeypatch.setattr(
        "agent.prompt_builder.build_context_files_prompt", lambda *a, **k: ""
    )
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    return seen


def test_multiplex_warmup_reads_default_profile_secrets(warmup_probe, monkeypatch):
    (get_hermes_home() / ".env").write_text("DISCORD_BOT_TOKEN=tok-default\n")
    monkeypatch.setattr("agent.secret_scope._MULTIPLEX_ACTIVE", True)

    assert gateway_run._warm_turn_machinery_sync() == 1

    # The gate saw the default profile's credential instead of raising.
    assert warmup_probe == [True]
    # The scope is torn down afterwards: unscoped reads still fail closed.
    with pytest.raises(secret_scope.UnscopedSecretError):
        secret_scope.get_secret("DISCORD_BOT_TOKEN")


def test_multiplex_warmup_never_borrows_process_env(warmup_probe, monkeypatch):
    # Under multiplexing os.environ may belong to another profile; a profile
    # without the credential must report the tool unavailable, not borrow it.
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok-other-profile")
    monkeypatch.setattr("agent.secret_scope._MULTIPLEX_ACTIVE", True)

    gateway_run._warm_turn_machinery_sync()

    assert warmup_probe == [False]


def test_single_profile_warmup_keeps_process_env(warmup_probe, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok-env")
    monkeypatch.setattr("agent.secret_scope._MULTIPLEX_ACTIVE", False)

    gateway_run._warm_turn_machinery_sync()

    assert warmup_probe == [True]
