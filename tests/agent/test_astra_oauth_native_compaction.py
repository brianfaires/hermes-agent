"""Astra native management requires both the supported model and OAuth destination."""

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.native_compaction import (
    native_compaction_context_management,
    resolve_native_compaction_capabilities,
)
from run_agent import AIAgent


EXACT_ROUTE_CASES = [
    ("https://chatgpt.com/backend-api/codex/", True),
    ("https://chatgpt.com:443/backend-api/codex", True),
    ("https://chatgpt.com/backend-api/codex/other", False),
    ("https://chatgpt.com/backend-api/codex/../other", False),
    ("https://chatgpt.com/backend-api/codex/%2e%2e/other", False),
    ("https://chatgpt.com/backend-api/codex?route=other", False),
    ("https://chatgpt.com/backend-api/codex?route=", False),
    ("https://chatgpt.com/backend-api/codex?route", False),
    ("https://chatgpt.com/backend-api/codex#other", False),
    ("https://chatgpt.com:8443/backend-api/codex", False),
    ("https://chatgpt.com:invalid/backend-api/codex", False),
    ("https://user:pass@chatgpt.com/backend-api/codex", False),
]


@pytest.mark.parametrize("base_url,eligible", EXACT_ROUTE_CASES)
def test_exact_astra_route_capability(base_url, eligible):
    assert resolve_native_compaction_capabilities(
        model="gpt-6-astra", provider="openai-codex",
        base_url=base_url, is_codex_backend=True,
    )["native_compaction"] is eligible


@pytest.mark.parametrize("base_url,eligible", [
    case for case in EXACT_ROUTE_CASES if ":invalid/" not in case[0]
])
def test_exact_astra_route_controls_request_and_replay(monkeypatch, base_url, eligible):
    Path(os.environ["HERMES_HOME"], "config.yaml").write_text(
        "compression:\n  codex_responses_native: true\n"
        "auxiliary:\n  title_generation:\n    enabled: false\n"
    )
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kwargs: [])
    agent = AIAgent(
        model="gpt-6-astra", provider="openai-codex", api_mode="codex_responses",
        base_url=base_url, api_key="synthetic-test-key",
        quiet_mode=True, skip_context_files=True, skip_memory=True,
        enabled_toolsets=[], save_trajectories=False,
    )
    try:
        request = agent._build_api_kwargs([
            {"role": "user", "content": "Earlier request"},
            {"role": "assistant", "content": "", "codex_reasoning_items": [{
                "type": "compaction", "encrypted_content": "synthetic-checkpoint",
                "_issuer_kind": "codex_backend",
            }]},
            {"role": "user", "content": "Continue"},
        ])
        assert ("context_management" in request) is eligible
        assert any(item.get("type") == "compaction" for item in request["input"]) is eligible
    finally:
        agent.close()


@pytest.mark.parametrize("model,provider,base_url,eligible", [
    ("gpt-6-astra", "openai-codex", "https://chatgpt.com/backend-api/codex", True),
    ("GPT-6-ASTRA", "openai-codex", "https://chatgpt.com:443/backend-api/codex/", True),
    ("gpt-6-astra", "openai", "https://api.openai.com/v1", False),
    ("gpt-6-astra", "openai", "", False),
    ("gpt-6-astra", "openai", "https://chatgpt.com/backend-api/codex", False),
    ("gpt-6-astra", "openai-codex", "https://relay.example/v1", False),
    ("gpt-6-astra", "openai-codex", "http://localhost:8080/v1", False),
    ("gpt-6-astra", "openai-codex", "https://chatgpt.com.example/backend-api/codex", False),
    ("gpt-6-astra", "openai-codex", "http://chatgpt.com/backend-api/codex", False),
    ("gpt-6-astra", "openai-codex", "https://chatgpt.com:8443/backend-api/codex", False),
    ("gpt-6-astra", "openai-codex", "https://chatgpt.com/backend-api/codex-other", False),
    ("gpt-6-astra", "openai-codex", "https://chatgpt.com:invalid/backend-api/codex", False),
    ("gpt-6-astra", "openai-codex", None, False),
    ("gpt-6-astra-mini", "openai-codex", "https://chatgpt.com/backend-api/codex", False),
    ("gpt-6-astra-900k", "openai-codex", "https://chatgpt.com/backend-api/codex", False),
    ("gpt-6-other", "openai-codex", "https://chatgpt.com/backend-api/codex", False),
    ("gpt-5.6-sol", "openai", "https://api.openai.com/v1", True),
    ("gpt-5.6-sol", "openai-codex", "https://chatgpt.com/backend-api/codex", True),
])
def test_destination_capability_and_request_gate_agree(model, provider, base_url, eligible):
    is_codex = provider == "openai-codex"
    resolved = resolve_native_compaction_capabilities(
        model=model, provider=provider, base_url=base_url, is_codex_backend=is_codex,
    )
    assert resolved["native_compaction"] is eligible
    agent = SimpleNamespace(
        model=model, provider=provider, base_url=base_url,
        codex_responses_native_compaction=True, compression_enabled=True,
        capabilities={"openai_native_compaction": True},
    )
    # The request gate must also exclude Astra relays before runtime resolution,
    # even when a proxy advertises support for native compaction.
    for runtime in (None, resolved):
        agent.runtime_capabilities = runtime
        payload = native_compaction_context_management(agent, is_codex_backend=is_codex)
        assert (payload is not None) is eligible


@pytest.mark.parametrize("setting,value,enabled", [
    (None, None, True),
    ("codex_responses_native_compaction", False, False),
    ("compression_enabled", False, False),
    ("compression_checkpoint_required", True, False),
    ("runtime_capabilities", {"native_compaction": False}, False),
])
def test_configured_agent_preserves_request_safety_gates(monkeypatch, setting, value, enabled):
    Path(os.environ["HERMES_HOME"], "config.yaml").write_text(
        "compression:\n  codex_responses_native: true\n"
        "  codex_responses_compact_threshold: 999999\n  threshold_tokens: 204000\n"
        "auxiliary:\n  title_generation:\n    enabled: false\n"
    )
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kwargs: [])
    agent: Any = AIAgent(
        model="gpt-6-astra", provider="openai-codex", api_mode="codex_responses",
        base_url="https://chatgpt.com/backend-api/codex", api_key="test-key",
        quiet_mode=True, skip_context_files=True, skip_memory=True,
        enabled_toolsets=[], save_trajectories=False,
    )
    try:
        assert agent.runtime_capabilities["native_compaction"] is True
        if setting:
            setattr(agent, setting, value)
        request = agent._build_api_kwargs([{"role": "user", "content": "Continue."}])
        assert ("context_management" in request) is enabled
        if enabled:
            management = request["context_management"][0]
            assert management["type"] == "compaction"
            assert 1024 <= management["compact_threshold"] < agent.context_compressor.threshold_tokens
    finally:
        agent.close()


@pytest.mark.parametrize("checkpoint_count", [1, 2])
def test_configured_astra_captures_replays_and_continues(monkeypatch, checkpoint_count):
    """Exercise the real loop/adapter/request builder across opaque checkpoints."""
    from agent.conversation_loop import _CODEX_INCOMPLETE_NUDGE

    Path(os.environ["HERMES_HOME"], "config.yaml").write_text(
        "compression:\n  codex_responses_native: true\n"
        "  threshold_tokens: 204000\n"
        "auxiliary:\n  title_generation:\n    enabled: false\n"
    )
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kwargs: [])
    agent = AIAgent(
        model="gpt-6-astra", provider="openai-codex", api_mode="codex_responses",
        base_url="https://chatgpt.com/backend-api/codex", api_key="synthetic-test-key",
        subscription_only=True, quiet_mode=True, skip_context_files=True,
        skip_memory=True, enabled_toolsets=[], save_trajectories=False,
        max_iterations=4,
    )
    sent = []

    def respond(request):
        sent.append(request)
        index = len(sent)
        if index <= checkpoint_count:
            output = [SimpleNamespace(type="compaction", status="completed",
                                      encrypted_content=f"synthetic-checkpoint-{index}")]
        else:
            output = [SimpleNamespace(type="message", status="completed", phase="final_answer",
                                      content=[SimpleNamespace(type="output_text", text="Done.")])]
        return SimpleNamespace(output=output, status="completed", model="gpt-6-astra",
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15))

    monkeypatch.setattr(agent, "_interruptible_api_call", respond)
    try:
        result = agent.run_conversation("Continue the investigation.")
        assert result["completed"] and result["final_response"] == "Done."
        assert len(sent) == checkpoint_count + 1
        for index, request in enumerate(sent):
            assert request["context_management"][0]["type"] == "compaction"
            checkpoints = [item for item in request["input"] if item.get("type") == "compaction"]
            if index:
                assert checkpoints[-1]["encrypted_content"] == f"synthetic-checkpoint-{index}"
                assert all("_issuer_kind" not in item for item in checkpoints)
            else:
                assert not checkpoints
        sidecars = [item for message in result["messages"]
                    for item in message.get("codex_reasoning_items", [])
                    if item.get("type") == "compaction"]
        assert sidecars and all(item["_issuer_kind"] == "codex_backend" for item in sidecars)
        nudges = [m for m in result["messages"] if m.get("content") == _CODEX_INCOMPLETE_NUDGE]
        assert len(nudges) == checkpoint_count - 1

        resumed = agent._build_api_kwargs(result["messages"] + [{"role": "user", "content": "Next step."}])
        assert any(item.get("type") == "compaction" for item in resumed["input"])
        agent.codex_responses_native_compaction = False
        disabled = agent._build_api_kwargs(result["messages"])
        assert "context_management" not in disabled
        assert not any(item.get("type") == "compaction" for item in disabled["input"])
    finally:
        agent.close()
