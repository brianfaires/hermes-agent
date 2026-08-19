from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_make_source(),
        message_id="m1",
        internal=True,
    )


def _session_entry() -> SessionEntry:
    return SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _session_entry()
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._queued_events = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._update_prompt_pending = {}
    runner._busy_input_mode = "interrupt"
    runner._draining = False
    runner._session_run_generation = {}
    runner._session_sources = {}
    runner._pending_native_image_paths_by_session = {}
    runner._background_tasks = {}
    runner._background_task_counter = 0
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._service_tier = None
    runner._fast_mode_by_session = {}
    runner._goal_state_by_session = {}
    runner._goal_runs_in_progress = set()
    runner._goal_queued_by_session = set()
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._should_send_telegram_lobby_reminder = lambda _source: False
    runner._check_slash_access = lambda _source, _command: None
    runner._begin_session_run_generation = lambda _key: 1
    runner._release_running_agent_state = lambda key: runner._running_agents.pop(key, None)
    return runner, adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("command_text", ["/queue do this next", "/q do this next"])
async def test_idle_queue_sends_payload_as_next_turn(command_text):
    runner, _adapter = _make_runner()
    captured = {}

    async def fake_handle_message_with_agent(event, source, key, generation):
        captured["text"] = event.text
        captured["command"] = event.get_command()
        captured["source"] = source
        captured["key"] = key
        captured["generation"] = generation
        return {"final_response": "", "messages": []}

    runner._handle_message_with_agent = fake_handle_message_with_agent

    result = await runner._handle_message(_make_event(command_text))

    assert result == {"final_response": "", "messages": []}
    assert captured["text"] == "do this next"
    assert captured["command"] is None
    assert captured["source"] == _make_source()
    assert captured["key"] == build_session_key(_make_source())
    assert captured["generation"] == 1
    assert runner._running_agents == {}


@pytest.mark.asyncio
async def test_idle_queue_without_payload_returns_usage():
    runner, _adapter = _make_runner()
    called = False

    async def fake_handle_message_with_agent(event, source, key, generation):
        nonlocal called
        called = True
        return {"final_response": "", "messages": []}

    runner._handle_message_with_agent = fake_handle_message_with_agent

    result = await runner._handle_message(_make_event("/queue"))

    assert result == "Usage: /queue <prompt>"
    assert called is False
    assert runner._running_agents == {}


@pytest.mark.asyncio
async def test_cold_inline_plugin_command_does_not_enter_agent_or_session_history(monkeypatch):
    from hermes_cli import plugins as plugins_mod

    runner, _adapter = _make_runner()
    received = []
    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_handler",
        lambda name: (lambda args: received.append(args) or "Logged id at 2026-08-19T00:00:00+00:00")
        if name == "log"
        else None,
    )
    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_metadata",
        lambda name: {
            "plugin": "private-journal",
            "inline_while_busy": True,
            "verbatim_args": True,
        } if name == "log" else None,
    )

    async def fail_agent(*_args, **_kwargs):
        raise AssertionError("journal command entered agent path")

    runner._handle_message_with_agent = fail_agent

    result = await runner._handle_message(_make_event("/log   raw private text  "))

    assert result == "Logged id at 2026-08-19T00:00:00+00:00"
    assert received == ["  raw private text  "]
    runner.session_store.load_transcript.assert_not_called()
    assert runner._running_agents == {}


@pytest.mark.asyncio
async def test_failed_private_plugin_command_does_not_fall_through_to_agent(monkeypatch):
    from hermes_cli import plugins as plugins_mod

    runner, _adapter = _make_runner()

    def fail_capture(_args):
        raise RuntimeError("holding store unavailable")

    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_handler",
        lambda name: fail_capture if name == "log" else None,
    )
    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_metadata",
        lambda name: {
            "plugin": "private-journal",
            "inline_while_busy": True,
            "verbatim_args": True,
        } if name == "log" else None,
    )

    async def fail_agent(*_args, **_kwargs):
        raise AssertionError("failed private command entered agent path")

    runner._handle_message_with_agent = fail_agent

    result = await runner._handle_message(_make_event("/log private sentinel"))

    assert result == "Plugin command failed."
    runner.session_store.load_transcript.assert_not_called()
    assert runner._running_agents == {}


@pytest.mark.asyncio
async def test_ordinary_followup_after_log_has_no_journal_text(monkeypatch):
    from hermes_cli import plugins as plugins_mod

    runner, _adapter = _make_runner()
    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_handler",
        lambda name: (lambda _args: "Logged id at 2026-08-19T00:00:00+00:00")
        if name == "log"
        else None,
    )
    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_metadata",
        lambda name: {
            "plugin": "private-journal",
            "inline_while_busy": True,
            "verbatim_args": True,
        } if name == "log" else None,
    )
    seen = []

    async def fake_handle_message_with_agent(event, *_args, **_kwargs):
        seen.append(event.text)
        return {"final_response": "ok", "messages": [{"role": "user", "content": event.text}]}

    runner._handle_message_with_agent = fake_handle_message_with_agent

    assert await runner._handle_message(_make_event("/log journal raw sentinel")) == "Logged id at 2026-08-19T00:00:00+00:00"
    assert await runner._handle_message(_make_event("ordinary follow-up")) == {
        "final_response": "ok",
        "messages": [{"role": "user", "content": "ordinary follow-up"}],
    }
    assert seen == ["ordinary follow-up"]
    assert "journal raw sentinel" not in repr(seen)


@pytest.mark.asyncio
async def test_cold_inline_plugin_command_respects_slash_access(monkeypatch):
    from hermes_cli import plugins as plugins_mod

    runner, _adapter = _make_runner()
    handler = MagicMock(return_value="should not run")
    runner._check_slash_access = lambda _source, _command: "denied"
    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_handler",
        lambda name: handler if name == "log" else None,
    )
    monkeypatch.setattr(
        plugins_mod,
        "get_plugin_command_metadata",
        lambda name: {
            "plugin": "private-journal",
            "inline_while_busy": True,
            "verbatim_args": True,
        } if name == "log" else None,
    )

    result = await runner._handle_message(_make_event("/log private"))

    assert result == "denied"
    handler.assert_not_called()
