import json
import stat
from types import SimpleNamespace

from agent.agent_runtime_helpers import dump_api_request_debug
from agent.request_capture import arm_request_capture, clear_request_captures, consume_request_capture


def test_one_shot_and_session_separation():
    clear_request_captures()
    arm_request_capture("a")
    assert not consume_request_capture("b")
    assert consume_request_capture("a")
    assert not consume_request_capture("a")


def test_capture_metadata_footprint_and_permissions(tmp_path):
    agent = SimpleNamespace(
        logs_dir=tmp_path, session_id="sid", base_url="https://example.test/v1?secret=x",
        api_mode="chat_completions", model="openai/gpt-5", provider="openai",
        client=SimpleNamespace(api_key="secret"), platform="telegram", _gateway_session_key="a",
        enabled_toolsets=["core"], disabled_toolsets=[], _use_prompt_caching=True,
        _use_native_cache_layout=False, ephemeral_system_prompt=None, prefill_messages=[],
        reasoning_config=None, request_overrides={}, verbose_logging=False, log_prefix="",
        _mask_api_key_for_logs=lambda value: "***", _vprint=lambda value: None,
    )
    path = dump_api_request_debug(agent, {
        "model": "gpt-5", "messages": [{"role": "user", "content": "hello"}],
        "tools": [{"type": "function", "function": {"name": "large", "description": "x" * 20}},
                  {"type": "function", "function": {"name": "small"}}],
    }, reason="one_shot_capture", capture=True)
    assert "with_tools" in path.name
    assert json.loads(path.read_text())["reason"] == "one_shot_capture"
    summary = path.with_suffix(".summary.txt")
    text = summary.read_text()
    assert "profile:" in text and "platform_source: telegram" in text
    assert "request_body_chars:" in text and text.index("large") < text.index("small")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(summary.stat().st_mode) == 0o600
