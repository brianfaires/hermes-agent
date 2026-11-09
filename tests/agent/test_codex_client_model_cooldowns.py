"""Codex client construction keeps pool cooldowns scoped to the requested model."""

import json
import time
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


MODEL = "gpt-test-current"
OTHER_MODEL = "gpt-test-other"
BASE_URL = "https://chatgpt.com/backend-api/codex"


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        json.dumps({"model": {"provider": "openai-codex", "default": MODEL}}),
        encoding="utf-8",
    )
    token = set_hermes_home_override(home)
    try:
        yield home
    finally:
        reset_hermes_home_override(token)


def _seed_pool(cooled_model):
    from hermes_cli.auth import write_credential_pool

    write_credential_pool("openai-codex", [
        {"id": f"entry-{index}", "source": "manual", "auth_type": "oauth", "priority": index,
         "access_token": f"fixture-token-{index}", "refresh_token": f"fixture-refresh-{index}",
         "base_url": BASE_URL, "model_cooldowns": {cooled_model: time.time() + 3600}}
        for index in range(2)
    ])


@pytest.mark.parametrize("raw_codex", [True, False], ids=["main", "auxiliary"])
@pytest.mark.parametrize("requested_model", [MODEL, f"openai/{MODEL}", f" {MODEL} "])
def test_other_model_cooldown_does_not_hide_codex_credentials(profile, raw_codex, requested_model):
    from agent.auxiliary_client import resolve_provider_client

    _seed_pool(OTHER_MODEL)
    client, model = resolve_provider_client("openai-codex", requested_model, raw_codex=raw_codex)
    assert client is not None, "an unrelated model's cooldown must not prevent client construction"
    try:
        assert model == MODEL
        assert client.api_key == "fixture-token-0"
        assert str(client.base_url).rstrip("/") == BASE_URL
    finally:
        client.close()


@pytest.mark.parametrize("raw_codex", [True, False], ids=["main", "auxiliary"])
@pytest.mark.parametrize("requested_model", [MODEL, f"openai/{MODEL}", f" {MODEL} "])
def test_requested_model_cooldown_still_blocks_codex_client(profile, raw_codex, requested_model):
    from agent.auxiliary_client import resolve_provider_client

    _seed_pool(MODEL)
    client, model = resolve_provider_client("openai-codex", requested_model, raw_codex=raw_codex)
    assert client is None
    assert model is None
