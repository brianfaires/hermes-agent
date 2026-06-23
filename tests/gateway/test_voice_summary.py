"""Tests for gateway-enforced voice summaries."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    SessionSource,
)
from gateway.voice_summary import maybe_build_voice_summary, voice_summary_enabled_for


def _builtin_hook_registry():
    from gateway.hooks import HookRegistry

    registry = HookRegistry()
    registry._register_builtin_hooks()
    return registry


class DummyAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)
        self.sent_text: list[tuple[str, str]] = []
        self.sent_voice: list[str] = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id, "type": "dm"}

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
        self.sent_text.append((chat_id, content))
        return SendResult(success=True, message_id="text-1")

    async def send_voice(self, chat_id: str, audio_path: str, caption=None, reply_to=None, **kwargs) -> SendResult:
        assert Path(audio_path).is_file()
        self.sent_voice.append(audio_path)
        return SendResult(success=True, message_id="voice-1")


@pytest.fixture
def event() -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        user_id="user-1",
        thread_id=None,
    )
    msg = MessageEvent(
        source=source,
        text="hello",
        message_type=MessageType.TEXT,
    )
    msg.message_id = "msg-1"
    return msg


def test_voice_summary_gate_is_separate_from_voice_auto_tts():
    config = {
        "voice": {"auto_tts": False},
        "voice_summary": {"enabled": True, "platforms": ["telegram"]},
    }

    assert voice_summary_enabled_for(platform=Platform.TELEGRAM, chat_id="chat-1", config=config)


@pytest.mark.asyncio
async def test_voice_summary_disabled_does_not_generate(monkeypatch):
    called = False

    def fake_tts(**kwargs):  # pragma: no cover - should not be called
        nonlocal called
        called = True
        return json.dumps({"success": True, "file_path": kwargs["output_path"]})

    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", fake_tts)

    result = await maybe_build_voice_summary(
        text="Should stay text-only",
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        thread_id=None,
        config={"voice_summary": {"enabled": False}},
        session_key="telegram:chat-1:user-1",
    )

    assert result is None
    assert called is False


@pytest.mark.asyncio
async def test_enabled_voice_summary_uses_summarizer(monkeypatch, tmp_path):
    seen_text = ""

    def fake_tts(*, text, output_path=None):
        nonlocal seen_text
        seen_text = text
        path = Path(output_path) if output_path else tmp_path / "summary.ogg"
        path.write_bytes(b"ogg-ish")
        return json.dumps({"success": True, "file_path": str(path)})

    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", fake_tts)

    audio = await maybe_build_voice_summary(
        text="Full response with too many details",
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        thread_id=None,
        config={"voice_summary": {"enabled": True, "platforms": ["telegram"]}},
        session_key="telegram:chat-1:user-1",
        summarize_fn=lambda _text: "Short spoken summary",
    )

    assert audio is not None
    assert seen_text == "Short spoken summary"
    Path(audio).unlink()


@pytest.mark.asyncio
async def test_summary_failure_falls_back_to_normalized_text(monkeypatch, tmp_path):
    seen_text = ""

    def fake_tts(*, text, output_path=None):
        nonlocal seen_text
        seen_text = text
        path = Path(output_path) if output_path else tmp_path / "summary.ogg"
        path.write_bytes(b"ogg-ish")
        return json.dumps({"success": True, "file_path": str(path)})

    async def broken_summary(_text):
        raise RuntimeError("tiny model tripped over its shoelaces")

    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", fake_tts)

    audio = await maybe_build_voice_summary(
        text="**Done**\n- shipped the thing",
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        thread_id=None,
        config={"voice_summary": {"enabled": True, "platforms": ["telegram"]}},
        session_key="telegram:chat-1:user-1",
        summarize_fn=broken_summary,
    )

    assert audio is not None
    assert Path(audio).is_file()
    assert "Done" in seen_text
    assert "shipped the thing" in seen_text
    Path(audio).unlink()


@pytest.mark.asyncio
async def test_agent_end_hook_schedules_voice_summary_after_text(monkeypatch, event):
    adapter = DummyAdapter()
    registry = _builtin_hook_registry()
    registry.gateway_runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})

    def fake_load_config():
        return {
            "voice": {"auto_tts": False},
            "voice_summary": {"enabled": True, "platforms": ["telegram"]},
        }

    def fake_tts(*, text, output_path=None):
        from gateway.session_context import get_session_env

        assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
        path = Path(output_path) if output_path else Path("/tmp/hermes_voice_summary_test.ogg")
        path.write_bytes(b"voice")
        return json.dumps({"success": True, "file_path": str(path)})

    monkeypatch.setattr("hermes_cli.config.load_config", fake_load_config)
    monkeypatch.setattr("gateway.voice_summary._default_summarize_text", lambda text, **_kwargs: text)
    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", fake_tts)

    await registry.emit("agent:end", {
        "platform": Platform.TELEGRAM,
        "chat_id": "chat-1",
        "thread_id": None,
        "session_key": "telegram:chat-1:user-1",
        "response_full": "**Done**\n- shipped it",
    })

    callback = adapter.pop_post_delivery_callback("telegram:chat-1:user-1")
    assert callback is not None

    await adapter.send("chat-1", "**Done**\n- shipped it")
    callback(delivery_succeeded=True)
    await asyncio.sleep(0.05)

    assert adapter.sent_text == [("chat-1", "**Done**\n- shipped it")]
    assert len(adapter.sent_voice) == 1
    assert not Path(adapter.sent_voice[0]).exists()


@pytest.mark.asyncio
async def test_agent_end_hook_skips_voice_summary_when_text_delivery_fails(monkeypatch):
    adapter = DummyAdapter()
    registry = _builtin_hook_registry()
    registry.gateway_runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"voice_summary": {"enabled": True, "platforms": ["telegram"]}},
    )
    monkeypatch.setattr("gateway.voice_summary._default_summarize_text", lambda text, **_kwargs: text)
    monkeypatch.setattr(
        "tools.tts_tool.text_to_speech_tool",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("TTS should not run")),
    )

    await registry.emit("agent:end", {
        "platform": Platform.TELEGRAM,
        "chat_id": "chat-1",
        "thread_id": None,
        "session_key": "telegram:chat-1:user-1",
        "response_full": "Visible text did not deliver",
    })

    callback = adapter.pop_post_delivery_callback("telegram:chat-1:user-1")
    assert callback is not None
    callback(delivery_succeeded=False)
    await asyncio.sleep(0.05)

    assert adapter.sent_voice == []


@pytest.mark.asyncio
async def test_tts_failure_does_not_block_text_delivery(monkeypatch, event):
    adapter = DummyAdapter()
    registry = _builtin_hook_registry()
    registry.gateway_runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"voice_summary": {"enabled": True, "platforms": ["telegram"]}},
    )
    monkeypatch.setattr("gateway.voice_summary._default_summarize_text", lambda text, **_kwargs: text)
    monkeypatch.setattr(
        "tools.tts_tool.text_to_speech_tool",
        lambda **_kwargs: json.dumps({"success": False, "error": "boom"}),
    )

    await registry.emit("agent:end", {
        "platform": Platform.TELEGRAM,
        "chat_id": "chat-1",
        "thread_id": None,
        "session_key": "telegram:chat-1:user-1",
        "response_full": "Visible text survives",
    })

    callback = adapter.pop_post_delivery_callback("telegram:chat-1:user-1")
    assert callback is not None

    await adapter.send("chat-1", "Visible text survives")
    callback(delivery_succeeded=True)
    await asyncio.sleep(0.05)

    assert adapter.sent_text == [("chat-1", "Visible text survives")]
    assert adapter.sent_voice == []
