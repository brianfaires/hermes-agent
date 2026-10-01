"""Scoped inference contracts: no provider I/O is permitted in these tests."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.inference_policy import (
    inference_scope, scoped_inference, subscription_only_active,
    validate_subscription_only,
)
from hermes_cli.fallback_config import get_fallback_chain


MODEL = "gpt-6.1-sol"
PROVIDER = "openai-codex"
CONFIG = {"fallback_providers": [{"provider": "openrouter", "model": "paid"}]}


def test_policy_is_scoped_and_preserves_global_fallback():
    original = get_fallback_chain(CONFIG)
    with inference_scope(True):
        assert get_fallback_chain(CONFIG) == []
        with inference_scope(False):
            assert subscription_only_active()
        with ThreadPoolExecutor(1) as executor:
            assert executor.submit(copy_context().run, subscription_only_active).result()
            assert not executor.submit(subscription_only_active).result()
    assert get_fallback_chain(CONFIG) == original


@pytest.mark.parametrize("value", ["false", 1, None])
def test_reject_non_boolean(value):
    with pytest.raises(ValueError):
        validate_subscription_only(value, PROVIDER, MODEL)
    @scoped_inference
    def entry(self, subscription_only=False):
        raise AssertionError("invalid policy reached execution")
    with pytest.raises(ValueError):
        entry(SimpleNamespace(subscription_only=False), subscription_only=value)


def test_resolution_rejected_before_config_or_credentials(monkeypatch):
    from hermes_cli import runtime_provider
    load = Mock(side_effect=AssertionError("must not read config"))
    monkeypatch.setattr(runtime_provider, "resolve_requested_provider", load)
    with inference_scope(True), pytest.raises(ValueError, match="subscription_only"):
        runtime_provider.resolve_runtime_provider(requested="openrouter", target_model="paid")
    load.assert_not_called()


def test_auxiliary_and_vision_never_construct_client(monkeypatch):
    from agent import auxiliary_client as aux
    construct = Mock(side_effect=AssertionError("unapproved client construction"))
    monkeypatch.setattr(aux, "_create_openai_client", construct)
    with inference_scope(True):
        with pytest.raises(RuntimeError, match="subscription_only"):
            aux.call_llm(task="compression", messages=[])
        with pytest.raises(RuntimeError, match="subscription_only"):
            aux._resolve_strict_vision_backend("openrouter")
        with pytest.raises(ValueError, match="subscription_only"):
            aux.resolve_provider_client("openrouter", "paid")
    construct.assert_not_called()


def test_in_loop_failover_is_disabled_even_with_injected_chain(monkeypatch):
    from agent.chat_completion_helpers import try_activate_fallback
    from agent import auxiliary_client
    construct = Mock(side_effect=AssertionError("unapproved client"))
    monkeypatch.setattr(auxiliary_client, "resolve_provider_client", construct)
    agent = SimpleNamespace(subscription_only=True, _fallback_chain=CONFIG["fallback_providers"])
    assert try_activate_fallback(agent) is False
    construct.assert_not_called()


def test_delegation_config_cannot_override_policy(monkeypatch):
    from tools.delegate_tool import _resolve_delegation_credentials
    from hermes_cli import runtime_provider
    resolve = Mock(side_effect=AssertionError("unapproved resolver"))
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", resolve)
    parent = SimpleNamespace(subscription_only=True, provider=PROVIDER, model=MODEL)
    with pytest.raises(ValueError, match="subscription_only"):
        _resolve_delegation_credentials({"provider": "openrouter", "model": "paid"}, parent)
    resolve.assert_not_called()


def test_cron_store_and_child_inheritance(tmp_path):
    from cron.jobs import create_job, get_job, update_job, use_cron_store

    @scoped_inference
    def create_children(subscription_only=False, provider=None, model=None):
        return create_job(prompt="child", schedule="30m")

    with use_cron_store(tmp_path):
        job = create_children(subscription_only=True, provider=PROVIDER, model=MODEL)
        stored = get_job(job["id"])
        assert stored["subscription_only"] is True
        assert (stored["provider"], stored["model"]) == (PROVIDER, MODEL)
        with pytest.raises(ValueError):
            update_job(job["id"], {"provider": "openrouter"})


@pytest.mark.parametrize("restricted", [False, True])
def test_scheduler_resolution_failure_retains_only_unrestricted_fallback(tmp_path, monkeypatch, restricted):
    import yaml
    from cron import scheduler
    from hermes_cli import runtime_provider
    from hermes_cli.auth import AuthError
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(CONFIG))
    calls = []

    def fail(**kwargs):
        calls.append(kwargs["requested"])
        raise AuthError("forced missing credentials")

    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", fail)
    result = scheduler.run_job({
        "id": "policy-test", "name": "policy", "prompt": "test", "schedule": "30m",
        "model": MODEL, "provider": PROVIDER, "subscription_only": restricted,
    })
    assert result[0] is False
    assert result[3]
    assert calls == ([PROVIDER] if restricted else [PROVIDER, "openrouter"])


def test_kanban_store_migration_inheritance_and_worker_command(tmp_path, monkeypatch):
    import subprocess
    from hermes_cli import kanban_db as kb
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    (home / "profiles" / "worker").mkdir(parents=True)
    kb.init_db()
    with kb.connect() as conn:
        unrestricted = kb.create_task(conn, title="normal")
        assert not kb.get_task(conn, unrestricted).subscription_only
        task_id = kb.create_task(conn, title="restricted", assignee="worker",
                                 subscription_only=True, provider_override=PROVIDER,
                                 model_override=MODEL)
        child = kb.create_task(conn, title="child", parents=[task_id])
        assert kb.get_task(conn, child).subscription_only
        with pytest.raises(ValueError):
            kb.set_model_override(conn, task_id, "paid", provider="openrouter")
        task = kb.get_task(conn, task_id)
        triage = kb.create_task(conn, title="split", triage=True, subscription_only=True,
                                provider_override=PROVIDER, model_override=MODEL)
        descendants = kb.decompose_triage_task(
            conn, triage, root_assignee="worker", children=[{"title": "part"}])
        assert descendants
        descendant = kb.get_task(conn, descendants[0])
        assert (descendant.subscription_only, descendant.provider_override,
                descendant.model_override) == (True, PROVIDER, MODEL)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    popen = Mock(return_value=SimpleNamespace(pid=4245))
    monkeypatch.setattr(subprocess, "Popen", popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kb._default_spawn(task, str(workspace))
    argv = popen.call_args.args[0]
    assert "--subscription-only" in argv
    assert argv[argv.index("--provider") + 1] == PROVIDER
    assert argv[argv.index("-m") + 1] == MODEL


@pytest.mark.parametrize("restricted", [False, True])
def test_cli_parser_and_preinit_failure(monkeypatch, restricted):
    pytest.importorskip("prompt_toolkit", reason="real CLI requires prompt_toolkit")
    from hermes_cli._parser import build_top_level_parser
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
    from hermes_cli import runtime_provider
    from hermes_cli.auth import AuthError
    parser, _, _ = build_top_level_parser()
    args = parser.parse_args(["chat", "--provider", PROVIDER, "-m", MODEL] +
                             (["--subscription-only"] if restricted else []))
    cli = SimpleNamespace(
        subscription_only=args.subscription_only, model=args.model,
        requested_provider=args.provider, _explicit_api_key=None,
        _explicit_base_url=None, _fallback_model=CONFIG["fallback_providers"],
    )
    resolve = Mock(side_effect=AuthError("forced authentication failure"))
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", resolve)
    assert CLIAgentSetupMixin._ensure_runtime_credentials(cli) is False
    assert resolve.call_args_list[0].kwargs["target_model"] == MODEL
    assert resolve.call_count == (1 if restricted else 2)


def test_compression_thread_fails_without_auxiliary_client(monkeypatch):
    from agent.context_compressor import ContextCompressor
    from agent import auxiliary_client
    compressor = object.__new__(ContextCompressor)
    compressor.subscription_only = True
    create = Mock(side_effect=AssertionError("unapproved construction"))
    monkeypatch.setattr(auxiliary_client, "_create_openai_client", create)
    with ThreadPoolExecutor(1) as executor:
        with pytest.raises(RuntimeError, match="subscription_only"):
            executor.submit(compressor.compress, [{"role": "user", "content": "hello"}]).result()
    create.assert_not_called()


@pytest.mark.parametrize("status,message", [(401, "authentication failed"), (429, "quota exceeded"),
                                            (400, "maximum context length exceeded")])
def test_real_agent_loop_failure_never_constructs_paid_client(monkeypatch, status, message):
    from unittest.mock import MagicMock, patch
    from run_agent import AIAgent
    from agent import auxiliary_client

    class ProviderFailure(Exception):
        status_code = status
        response = SimpleNamespace(headers={})
        body = {"error": {"message": message}}

    constructions = []
    def construct(**kwargs):
        constructions.append(kwargs.get("base_url"))
        assert kwargs.get("base_url", "").rstrip("/") == "https://chatgpt.com/backend-api/codex"
        return MagicMock()

    with (patch("run_agent.OpenAI", side_effect=construct),
          patch("run_agent.get_tool_definitions", return_value=[]),
          patch("run_agent.check_toolset_requirements", return_value={}),
          patch("agent.model_metadata.get_model_context_length", return_value=200000)):
        agent = AIAgent(provider=PROVIDER, model=MODEL, api_key="test-only",
                        base_url="https://chatgpt.com/backend-api/codex",
                        subscription_only=True, quiet_mode=True, skip_context_files=True,
                        fallback_model=CONFIG["fallback_providers"], max_iterations=2)
        assert agent._fallback_chain == []
        agent._api_max_retries = 1
        agent._cached_system_prompt = "Test"
        agent.compression_enabled = False
        agent._credential_pool = None
        calls = Mock(side_effect=ProviderFailure(message))
        monkeypatch.setattr(agent, "_interruptible_api_call", calls)
        monkeypatch.setattr(agent, "_try_recover_primary_transport", lambda *a, **k: False)
        monkeypatch.setattr(agent, "_try_refresh_codex_client_credentials", lambda **kw: False)
        monkeypatch.setattr(agent, "_cleanup_task_resources", lambda *a, **k: None)
        resolve = Mock(side_effect=AssertionError("unapproved fallback construction"))
        monkeypatch.setattr(auxiliary_client, "resolve_provider_client", resolve)
        with patch("agent.agent_runtime_helpers.time.sleep"):
            result = agent.run_conversation("hello")
        assert calls.called
        assert not result.get("completed", False)
        resolve.assert_not_called()
        assert constructions


def test_pool_endpoint_rejected_before_client_construction(monkeypatch):
    from run_agent import AIAgent
    agent = object.__new__(AIAgent)
    agent.subscription_only = True
    agent.provider, agent.model = PROVIDER, MODEL
    agent.base_url = "https://chatgpt.com/backend-api/codex"
    with pytest.raises(ValueError, match="subscription_only"):
        agent._swap_credential(SimpleNamespace(runtime_base_url="https://api.openai.com/v1"))
    assert agent.base_url == "https://chatgpt.com/backend-api/codex"


def test_cli_parser_retains_explicit_subscription_route():
    from hermes_cli._parser import build_top_level_parser
    parser, _, _ = build_top_level_parser()
    args = parser.parse_args(["chat", "--subscription-only", "--provider", PROVIDER, "-m", MODEL])
    assert (args.subscription_only, args.provider, args.model) == (True, PROVIDER, MODEL)


@pytest.mark.parametrize("restricted", [False, True])
def test_speech_policy_blocks_before_paid_dispatch(tmp_path, monkeypatch, restricted):
    import json
    from tools import tts_tool
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "openai"})
    paid = Mock(side_effect=RuntimeError("paid dispatch reached"))
    monkeypatch.setattr(tts_tool, "_generate_openai_tts", paid)
    with inference_scope(restricted):
        result = json.loads(tts_tool.text_to_speech_tool(
            "Hello", output_path=str(tmp_path / "speech.mp3"), provider="openai"))
    if restricted:
        assert "subscription_only" in result.get("error", "")
        paid.assert_not_called()
    else:
        paid.assert_called_once()
        assert "paid dispatch reached" in result.get("error", "")


@pytest.mark.parametrize("restricted", [False, True])
def test_cron_cli_inherits_persisted_worker_policy(tmp_path, monkeypatch, restricted):
    from hermes_cli import kanban_db as kb
    from hermes_cli.cron import cron_create
    from cron.jobs import list_jobs, use_cron_store
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="parent", subscription_only=restricted,
                                 provider_override=PROVIDER, model_override=MODEL)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
    assert not subscription_only_active()  # Fresh CLI process has no parent ContextVar.
    with use_cron_store(home):
        assert cron_create(SimpleNamespace(schedule="30m", prompt="child")) == 0
        job = list_jobs()[0]
    assert job["subscription_only"] is restricted
    if restricted:
        assert (job["provider"], job["model"]) == (PROVIDER, MODEL)
        from cron.jobs import create_job
        with use_cron_store(home):
            with pytest.raises(ValueError, match="subscription_only"):
                create_job("bad override", "30m", provider="openai", model=MODEL)
            with pytest.raises(ValueError, match="boolean"):
                create_job("bad boolean", "30m", subscription_only="false")
            assert len(list_jobs()) == 1
    else:
        assert job["provider"] is None and job["model"] is None


def test_speech_stream_and_chunk_reject_before_loading_provider(monkeypatch):
    import json
    import queue
    import threading
    from tools import tts_tool
    load = Mock(side_effect=AssertionError("must not resolve speech provider"))
    monkeypatch.setattr(tts_tool, "_load_tts_config", load)
    done = threading.Event()
    with inference_scope(True):
        result = json.loads(tts_tool._text_to_speech_single("hello", provider="openai"))
        assert "subscription_only" in result["error"]
        with pytest.raises(RuntimeError, match="subscription_only"):
            tts_tool.stream_tts_to_speaker(queue.Queue(), threading.Event(), done,
                                          provider="openai")
    assert done.is_set()
    load.assert_not_called()
