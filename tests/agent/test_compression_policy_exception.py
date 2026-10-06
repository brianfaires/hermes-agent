"""Compression uses its configured route without relaxing other inference policy."""
import json
import os
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import yaml

from agent import auxiliary_client as aux
from agent.inference_policy import inference_scope, subscription_only_active
from run_agent import AIAgent


@pytest.fixture
def configured(monkeypatch, request):
    expected_restricted = getattr(request.node, "callspec", None)
    expected_restricted = (
        expected_restricted.params.get("restricted", True) if expected_restricted else True
    )
    config = {
        "compression": {"threshold_tokens": 12000, "protect_first_n": 1,
                        "protect_last_n": 2, "codex_responses_native": True},
        "auxiliary": {"free_only": True,
                      "compression": {"provider": "openrouter", "model": "test/paid-summary",
                                      "api_key": "synthetic-test-key"},
                      "title_generation": {"enabled": False}},
    }
    Path(os.environ["HERMES_HOME"], "config.yaml").write_text(yaml.safe_dump(config))
    aux.shutdown_cached_clients()
    requests = []

    def respond(request):
        # Reentrant callbacks must still observe the original restriction.
        assert subscription_only_active() is expected_restricted
        for task in ("vision", "title_generation", None):
            if subscription_only_active():
                with pytest.raises(RuntimeError, match="subscription_only"):
                    aux.call_llm(task=task, messages=[])
            else:
                denied, _ = aux._get_cached_client(
                    "openrouter", "test/paid-summary", task=task,
                    api_key="synthetic-test-key",
                )
                assert denied is None
        if subscription_only_active():
            with pytest.raises(ValueError, match="subscription_only"):
                aux.resolve_provider_client("openrouter", "other/paid")
            with pytest.raises(ValueError, match="subscription_only"):
                aux._create_openai_client(api_key="synthetic-test-key", base_url="https://openrouter.ai/api/v1")
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == "test/paid-summary"
        payload = {
            "id": "test-summary", "object": "chat.completion", "created": 0,
            "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "Completed earlier checks; continue the pending task."}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 12, "total_tokens": 112},
        }
        if body.get("stream"):
            chunk = dict(payload, object="chat.completion.chunk", choices=[{
                "index": 0, "finish_reason": "stop",
                "delta": {"role": "assistant", "content": payload["choices"][0]["message"]["content"]},
            }])
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json=payload)

    async def async_respond(request):
        if subscription_only_active():
            for task in ("title_generation", "vision"):
                with pytest.raises(RuntimeError, match="subscription_only"):
                    await aux.async_call_llm(task=task, messages=[])
        return respond(request)

    def transport(base_url, async_mode=False):
        cls = httpx.AsyncClient if async_mode else httpx.Client
        handler = async_respond if async_mode else respond
        return {"http_client": cls(transport=httpx.MockTransport(handler), trust_env=False)}

    # Precompute SDK platform headers so this transport test does not start
    # an unrelated platform-detection executor.
    import openai
    from openai._base_client import get_platform
    async_client = openai.AsyncOpenAI

    def make_async_client(**kwargs):
        client = async_client(**kwargs)
        client._platform = get_platform()
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", make_async_client)
    monkeypatch.setattr(aux, "_openai_http_client_kwargs", transport)
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *a, **kw: 256000)
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kw: [])
    yield requests
    aux.shutdown_cached_clients()


def make_agent():
    return AIAgent(
        model="gpt-6-astra", provider="openai-codex", api_mode="codex_responses",
        base_url="https://chatgpt.com/backend-api/codex", api_key="synthetic-test-key",
        subscription_only=True, quiet_mode=True, skip_context_files=True,
        skip_memory=True, enabled_toolsets=[], save_trajectories=False,
    )


def history():
    return [{"role": "user" if i % 2 == 0 else "assistant",
             "content": f"Turn {i}: " + "earlier task details " * 700} for i in range(18)]


@pytest.mark.parametrize("kanban", [False, True])
@pytest.mark.parametrize("ambient", [False, True])
def test_real_restricted_agent_compresses_configured_paid_route(configured, monkeypatch, kanban, ambient):
    if kanban:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "synthetic-task")
    park = Mock(side_effect=AssertionError("compression must not park"))
    monkeypatch.setattr("hermes_cli.kanban_worker_context.park_for_context", park)
    agent = make_agent()
    original = history()
    try:
        with inference_scope(ambient):
            compressed, _ = agent._compress_context(original, "Test system", force=True,
                                                     approx_tokens=40000)
            assert subscription_only_active() is ambient
        assert configured, "real compressor must reach the configured provider transport"
        assert agent.context_compressor.compression_count > 0
        assert len(compressed) < len(original)
        assert not agent.context_compressor._last_compress_aborted
        assert agent.subscription_only is True
        park.assert_not_called()
    finally:
        agent.close()


def test_sync_compression_keeps_nested_tasks_restricted(configured):
    with inference_scope(True):
        assert aux.call_llm(task="compression", messages=[{"role": "user", "content": "Summarize"}])
        assert subscription_only_active()
    assert len(configured) == 1
    assert not subscription_only_active()


@pytest.mark.asyncio
async def test_async_compression_keeps_nested_tasks_restricted(configured):
    with inference_scope(True):
        result = await aux.async_call_llm(task="compression", messages=[{"role": "user", "content": "Summarize"}])
        assert result.choices[0].message.content
        for task in ("vision", "title_generation", None):
            with pytest.raises(RuntimeError, match="subscription_only"):
                await aux.async_call_llm(task=task, messages=[])
        assert subscription_only_active()
    assert len(configured) == 1
    assert not subscription_only_active()


def test_free_only_exception_does_not_cover_other_routes_or_tasks(configured):
    client, model = aux.get_text_auxiliary_client("compression")
    assert client is not None and model == "test/paid-summary"
    cached, _ = aux._get_cached_client("openrouter", "test/paid-summary", task="compression",
                                       api_key="synthetic-test-key")
    assert cached is not None
    denied, _ = aux._get_cached_client("openrouter", "test/paid-summary", task="title_generation",
                                       api_key="synthetic-test-key")
    assert denied is None
    for task, model in (("title_generation", "test/paid-summary"),
                        ("compression", "other/paid"), (None, "test/paid-summary")):
        client, _ = aux.resolve_provider_client("openrouter", model, task=task,
                                               explicit_api_key="synthetic-test-key")
        assert client is None
    assert not configured


def test_restricted_micro_compaction_reaches_configured_route(configured):
    agent = make_agent()
    try:
        comp = agent.context_compressor
        comp._micro_compact_enabled = True
        comp._micro_compact_every_n_turns = 1
        original = history()
        before_chars = sum(len(m["content"]) for m in original)
        with inference_scope(True):
            compacted = comp._micro_compact(original)
            assert subscription_only_active()
        assert configured
        assert sum(len(m["content"]) for m in compacted) < before_chars
        assert comp._micro_compact_passes > 0
    finally:
        agent.close()


def test_configured_custom_compression_route_retains_ambient_policy(configured):
    path = Path(os.environ["HERMES_HOME"], "config.yaml")
    config = yaml.safe_load(path.read_text())
    config["auxiliary"]["compression"].update(provider="custom", base_url="https://summary.example/v1")
    path.write_text(yaml.safe_dump(config))
    with inference_scope(True):
        result = aux.call_llm(task="compression", messages=[{"role": "user", "content": "Summarize"}])
        assert result.choices[0].message.content
        assert subscription_only_active()
    assert len(configured) == 1


@pytest.mark.parametrize("restricted", [False, True])
def test_auto_compression_inherits_configured_paid_main_route(configured, monkeypatch, restricted):
    path = Path(os.environ["HERMES_HOME"], "config.yaml")
    config = yaml.safe_load(path.read_text())
    config["model"] = {"provider": "openrouter", "default": "test/paid-summary"}
    config["auxiliary"]["compression"] = {"provider": "auto", "model": ""}
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    with inference_scope(restricted):
        client, model = aux.get_text_auxiliary_client("compression")
        assert client is not None
        assert model == "test/paid-summary"
        assert subscription_only_active() is restricted
    assert not configured


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("restricted", [False, True])
@pytest.mark.parametrize("provider", ["openrouter", "auto"])
async def test_same_provider_compression_rebuild_reaches_transport(
    configured, provider, restricted, async_mode,
):
    runtime = {"provider": "openrouter", "model": "test/paid-summary",
               "base_url": "https://openrouter.ai/api/v1",
               "api_key": "synthetic-test-key"}
    if provider == "auto":
        path = Path(os.environ["HERMES_HOME"], "config.yaml")
        config = yaml.safe_load(path.read_text())
        # The runtime must survive independently of the on-disk main route.
        config["model"] = {"provider": "openrouter", "default": "other/paid"}
        config["auxiliary"]["compression"] = {"provider": "auto", "model": ""}
        path.write_text(yaml.safe_dump(config))
    kwargs = dict(
        task="compression", resolved_provider=provider,
        resolved_model="test/paid-summary" if provider != "auto" else None,
        resolved_base_url=None, resolved_api_key="synthetic-test-key",
        resolved_api_mode=None, final_model="test/paid-summary",
        messages=[{"role": "user", "content": "Summarize"}],
        temperature=None, max_tokens=128, tools=None,
        effective_timeout=10, effective_extra_body={}, reasoning_config=None,
    )
    with inference_scope(restricted):
        # Model the cache eviction performed by credential recovery.
        client, _ = aux._get_cached_client(
            provider, kwargs["resolved_model"], async_mode=async_mode,
            api_key="synthetic-test-key", task="compression", main_runtime=runtime,
        )
        assert client is not None
        aux._evict_cached_clients(provider)
        if async_mode:
            result = await aux._retry_same_provider_async(**kwargs, main_runtime=runtime)
        else:
            result = aux._retry_same_provider_sync(**kwargs, main_runtime=runtime)
        assert result.choices[0].message.content
        assert subscription_only_active() is restricted
    assert len(configured) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("restricted", [False, True])
async def test_compression_pool_recovery_preserves_explicit_runtime(
    configured, monkeypatch, async_mode, restricted,
):
    path = Path(os.environ["HERMES_HOME"], "config.yaml")
    config = yaml.safe_load(path.read_text())
    config["model"] = {"provider": "openrouter", "default": "other/paid"}
    config["auxiliary"]["compression"] = {"provider": "auto", "model": ""}
    path.write_text(yaml.safe_dump(config))
    runtime = {"provider": "openrouter", "model": "test/paid-summary",
               "base_url": "https://openrouter.ai/api/v1",
               "api_key": "synthetic-test-key"}
    attempts = []
    recoveries = []

    def expired(request):
        attempts.append(str(request.url))
        if len(attempts) == 1:
            return httpx.Response(402, json={"error": {"message": "exhausted synthetic credential"}})
        return None

    sync_handle = httpx.MockTransport.handle_request
    async_handle = httpx.MockTransport.handle_async_request

    def handle(self, request):
        return expired(request) or sync_handle(self, request)

    async def async_handle_request(self, request):
        return expired(request) or await async_handle(self, request)

    def refresh(provider, error, **kwargs):
        recoveries.append(provider)
        aux._evict_cached_clients(provider)
        return True

    monkeypatch.setattr(httpx.MockTransport, "handle_request", handle)
    monkeypatch.setattr(httpx.MockTransport, "handle_async_request", async_handle_request)
    monkeypatch.setattr(aux, "_recoverable_pool_provider", lambda *args, **kwargs: "openrouter")
    monkeypatch.setattr(aux, "_recover_provider_pool", refresh)
    with inference_scope(restricted):
        kwargs = dict(task="compression", main_runtime=runtime,
                      messages=[{"role": "user", "content": "Summarize"}])
        result = await aux.async_call_llm(**kwargs) if async_mode else aux.call_llm(**kwargs)
        assert result.choices[0].message.content
        assert subscription_only_active() is restricted
    assert recoveries == ["openrouter"]
    assert attempts == ["https://openrouter.ai/api/v1/chat/completions"] * 2
    assert len(configured) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
async def test_nous_compression_refresh_keeps_task_cache_partition(configured, monkeypatch, async_mode):
    monkeypatch.setattr(aux, "_resolve_nous_runtime_api",
                        lambda **kwargs: ("synthetic-test-key", "https://inference-api.nousresearch.com/v1"))
    with inference_scope(True):
        client, model = aux._refresh_nous_auxiliary_client(
            cache_provider="nous", model="test/paid-summary",
            async_mode=async_mode, task="compression",
        )
        kwargs = {"model": model, "messages": [{"role": "user", "content": "Summarize"}]}
        if async_mode:
            result = await client.chat.completions.create(**kwargs)
        else:
            result = client.chat.completions.create(**kwargs)
        assert result.choices[0].message.content
        cached, _ = aux._get_cached_client(
            "nous", model, async_mode=async_mode, task="compression",
        )
        assert cached is client
        with pytest.raises(ValueError, match="subscription_only"):
            aux._get_cached_client("nous", model, async_mode=async_mode, task="title_generation")
    assert len(configured) == 1
