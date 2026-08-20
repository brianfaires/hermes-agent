from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.agent_runtime_helpers import invoke_tool
from agent.tool_executor import _latest_user_message_text


def test_concurrent_memory_tool_receives_latest_user_source():
    memory_manager = MagicMock()
    memory_manager.has_tool.return_value = True
    memory_manager.handle_tool_call.return_value = '{"result":"blocked"}'
    agent = SimpleNamespace(
        _memory_manager=memory_manager,
        session_id="session-1",
        valid_tool_names={"hindsight_retain"},
        enabled_toolsets=None,
        disabled_toolsets=None,
        _persist_user_message_idx=0,
    )
    messages = [
        {"role": "user", "content": "Log: private family event"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "Synthetic continuation instruction"},
    ]

    invoke_tool(
        agent,
        "hindsight_retain",
        {"content": "transformed summary"},
        "task-1",
        messages=messages,
    )

    memory_manager.handle_tool_call.assert_called_once_with(
        "hindsight_retain",
        {"content": "transformed summary"},
        source_user_content="Log: private family event",
    )


def test_latest_user_message_text_ignores_tool_results_after_user_turn():
    messages = [
        {"role": "user", "content": "Quick log: private family event"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "content": "tool result"},
    ]

    assert _latest_user_message_text(messages) == "Quick log: private family event"


def test_latest_user_message_text_flattens_multimodal_text_parts():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Log entry. private family event"},
                {"type": "image_url", "image_url": {"url": "https://example.invalid/x"}},
            ],
        }
    ]

    assert _latest_user_message_text(messages) == "Log entry. private family event"
