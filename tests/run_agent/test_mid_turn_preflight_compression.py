"""Regression test: post-tool compression must account for newly appended tool output."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _mock_tool_call(name="web_search", arguments="{}", call_id="call_1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _mock_response(content="Hello", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_agent() -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=6,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = True
    agent.save_trajectories = False
    agent.context_compressor = SimpleNamespace(
        threshold_tokens=100,
        protect_first_n=0,
        protect_last_n=0,
        last_prompt_tokens=50,
        should_compress=lambda tokens: tokens >= 100,
    )
    return agent


def test_post_tool_compression_counts_new_tool_output_even_with_stale_prompt_tokens():
    agent = _make_agent()
    tool_args = {"query": "same"}
    responses = [
        _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[_mock_tool_call("web_search", json.dumps(tool_args), "c1")],
        ),
        _mock_response(content="done", finish_reason="stop", tool_calls=None),
    ]
    agent.client.chat.completions.create.side_effect = responses

    compressed_lengths = []

    def _fake_compress(messages, system_message, *, approx_tokens=None, task_id=None, focus_topic=None):
        compressed_lengths.append((len(messages), approx_tokens))
        # Keep the latest user request + tool result, but drop one older turn so
        # the call is observably smaller and the loop restarts without hitting
        # the giant follow-up API request that triggered the regression.
        return messages[1:], system_message

    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"ok": True})),
        patch("run_agent.estimate_request_tokens_rough", side_effect=[10, 150, 10, 10]),
        patch.object(agent, "_compress_context", side_effect=_fake_compress) as mock_compress,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("search repeatedly")

    assert result["completed"] is True
    assert result["final_response"] == "done"
    assert result["api_calls"] == 2
    assert mock_compress.call_count >= 1
    assert compressed_lengths[0][1] > agent.context_compressor.last_prompt_tokens
