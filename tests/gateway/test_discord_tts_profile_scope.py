import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override


class _DiscordVoiceAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.DISCORD)
        self.played = []

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content=None, **kwargs):
        return SendResult(success=True, message_id="text")

    async def play_tts(self, chat_id, audio_path, **kwargs):
        self.played.append((chat_id, audio_path, kwargs))
        return SendResult(success=True, message_id="voice")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "channel"}


def _voice_event(profile="ops"):
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="discord-text",
        chat_type="channel",
        user_id="user-1",
        profile=profile,
    )
    return MessageEvent(
        text="spoken input",
        message_type=MessageType.VOICE,
        source=source,
        message_id="msg-1",
    )


@pytest.mark.asyncio
async def test_discord_voice_auto_tts_uses_named_profile_scope_and_output_path(tmp_path, monkeypatch):
    default_home = tmp_path / "default-home"
    ops_home = tmp_path / "profiles" / "ops"
    default_home.mkdir(parents=True)
    ops_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    adapter = _DiscordVoiceAdapter()
    adapter.set_runtime_profile_home(ops_home)
    adapter._should_auto_tts_for_chat = lambda chat_id: True
    adapter._message_handler = AsyncMock(return_value="**Ops voice reply**")

    captured = {}

    def fake_tts(text, output_path=None):
        captured["home"] = str(get_hermes_home())
        captured["profile_label"] = Path(get_hermes_home()).name
        captured["text"] = text
        captured["output_path"] = output_path
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"audio")
        return json.dumps({"success": True, "file_path": output_path})

    monkeypatch.setattr("tools.tts_tool.check_tts_requirements", lambda: True)
    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", fake_tts)

    event = _voice_event()
    await adapter._process_message_background(event, build_session_key(event.source))

    assert captured["home"] == str(ops_home.resolve())
    assert captured["profile_label"] == "ops"
    assert captured["text"] == "Ops voice reply"
    assert "/hermes_voice/ops/" in captured["output_path"]
    assert captured["output_path"].endswith(".mp3")
    assert adapter.played and adapter.played[0][0] == "discord-text"


@pytest.mark.asyncio
async def test_final_voice_reply_tts_runs_inside_source_profile_scope(tmp_path, monkeypatch):
    default_home = tmp_path / "default-home"
    ops_home = tmp_path / "profiles" / "ops"
    default_home.mkdir(parents=True)
    ops_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._gateway_profile_home = ops_home.resolve()
    runner._gateway_profile_name = "ops"
    runner.adapters = {Platform.DISCORD: SimpleNamespace(
        is_connected=True,
        _running=True,
        play_in_voice_channel=AsyncMock(),
        is_in_voice_channel=lambda guild_id: False,
        send_voice=AsyncMock(return_value=SendResult(success=True, message_id="voice")),
    )}
    runner._get_guild_id = lambda event: None
    runner._reply_anchor_for_event = lambda event: "msg-1"
    runner._thread_metadata_for_source = lambda source, anchor=None: {"thread_id": source.thread_id} if source.thread_id else None

    captured = {}

    def fake_tts(text, output_path=None):
        captured["home"] = str(get_hermes_home())
        captured["output_path"] = output_path
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"audio")
        return json.dumps({"success": True, "file_path": output_path})

    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", fake_tts)
    monkeypatch.setattr("tools.tts_tool._strip_markdown_for_tts", lambda text: text.replace("**", "").strip())

    event = _voice_event(profile="ops")
    event.source.thread_id = "thread-1"
    await GatewayRunner._send_voice_reply(runner, event, "**final reply**")

    assert captured["home"] == str(ops_home.resolve())
    assert "/hermes_voice/ops/" in captured["output_path"]
    runner.adapters[Platform.DISCORD].send_voice.assert_awaited_once()


@pytest.mark.asyncio
async def test_final_voice_reply_uses_adapter_play_tts_when_no_raw_guild(tmp_path, monkeypatch):
    ops_home = tmp_path / "profiles" / "ops"
    ops_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(ops_home))

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._gateway_profile_home = ops_home.resolve()
    runner._gateway_profile_name = "ops"
    adapter = SimpleNamespace(
        is_connected=True,
        _running=True,
        play_tts=AsyncMock(return_value=SendResult(success=True, message_id="voice")),
        send_voice=AsyncMock(return_value=SendResult(success=True, message_id="file")),
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._get_guild_id = lambda event: None
    runner._reply_anchor_for_event = lambda event: "msg-1"
    runner._thread_metadata_for_source = lambda source, anchor=None: {"thread_id": source.thread_id} if source.thread_id else None

    def fake_tts(text, output_path=None):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"audio")
        return json.dumps({"success": True, "file_path": output_path})

    monkeypatch.setattr("tools.tts_tool.text_to_speech_tool", fake_tts)
    monkeypatch.setattr("tools.tts_tool._strip_markdown_for_tts", lambda text: text.strip())

    event = _voice_event(profile="ops")
    event.source.thread_id = "thread-1"
    await GatewayRunner._send_voice_reply(runner, event, "linked voice prompt")

    adapter.play_tts.assert_awaited_once()
    assert adapter.play_tts.await_args.kwargs["chat_id"] == "discord-text"
    assert adapter.play_tts.await_args.kwargs["metadata"] == {"thread_id": "thread-1", "notify": True}
    adapter.send_voice.assert_not_awaited()


def test_tts_default_output_dir_resolves_at_call_time(tmp_path, monkeypatch):
    import tools.tts_tool as tts_tool

    default_home = tmp_path / "default-home"
    ops_home = tmp_path / "profiles" / "ops"
    default_home.mkdir(parents=True)
    ops_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    token = set_hermes_home_override(str(ops_home))
    try:
        resolved = tts_tool._get_default_output_dir()
    finally:
        reset_hermes_home_override(token)

    assert str(ops_home) in resolved
    assert str(default_home) not in resolved
