"""Tests for Discord auto-managed hands-free voice channel presence."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from plugins.platforms.discord.adapter import DiscordAdapter


def _guild(guild_id=42):
    return SimpleNamespace(id=guild_id)


def _member(member_id=123, guild=None, *, bot=False):
    return SimpleNamespace(
        id=member_id,
        display_name="Brian" if member_id == 123 else f"User {member_id}",
        guild=guild or _guild(),
        bot=bot,
    )


def _channel(channel_id=456, guild=None, members=None):
    guild = guild or _guild()
    return SimpleNamespace(
        id=channel_id,
        name="Commute",
        guild=guild,
        members=members if members is not None else [],
    )


def _state(channel):
    return SimpleNamespace(channel=channel)


def _runner_adapter():
    runner = object.__new__(GatewayRunner)
    runner._voice_mode = {}
    runner._running_agents = {}
    runner._save_voice_modes = MagicMock()

    adapter = SimpleNamespace()
    adapter.join_voice_channel = AsyncMock(return_value=True)
    adapter.leave_voice_channel = AsyncMock()
    adapter._voice_text_channels = {}
    adapter._auto_voice_session_channels = set()
    adapter._voice_sources = {}
    adapter._auto_tts_enabled_chats = set()
    adapter._auto_tts_disabled_chats = set()
    adapter._voice_input_callback = None
    adapter._on_voice_disconnect = None
    adapter._voice_clients = {}
    adapter.is_in_voice_channel = MagicMock(return_value=True)
    runner._send_voice_reply = AsyncMock()
    return runner, adapter


@pytest.mark.asyncio
async def test_discord_auto_voice_join_enables_voice_only_and_links_text_channel():
    runner, adapter = _runner_adapter()
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild, members=[member])

    result = await runner._handle_discord_auto_voice_join(adapter, member, channel)

    assert result is True
    adapter.join_voice_channel.assert_awaited_once_with(channel)
    assert adapter._voice_text_channels[42] == 456
    assert adapter._auto_voice_session_channels == {"456"}
    assert adapter._voice_sources[42]["platform"] == "discord"
    assert adapter._voice_sources[42]["chat_id"] == "456"
    assert adapter._voice_sources[42]["chat_type"] == "group"
    assert adapter._voice_sources[42]["user_id"] == "123"
    text_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="456",
        chat_type="group",
        user_id="123",
    )
    voice_source = SessionSource.from_dict(adapter._voice_sources[42])
    assert build_session_key(voice_source) == build_session_key(text_source)
    assert runner._voice_mode["discord:456"] == "voice_only"
    runner._save_voice_modes.assert_called_once()
    assert "456" in adapter._auto_tts_enabled_chats
    assert "456" not in adapter._auto_tts_disabled_chats
    assert adapter._voice_input_callback.__func__ is runner._handle_voice_channel_input.__func__
    assert adapter._on_voice_disconnect.__func__ is runner._handle_voice_timeout_cleanup.__func__
    runner._send_voice_reply.assert_awaited_once()
    greeting_event, greeting_text = runner._send_voice_reply.await_args.args
    assert greeting_event.source.platform == Platform.DISCORD
    assert greeting_event.source.chat_id == "456"
    assert greeting_text == "I'm here. What's up?"


@pytest.mark.asyncio
async def test_discord_auto_voice_join_uses_configured_join_ack_phrase():
    runner, adapter = _runner_adapter()
    adapter._voice_fx_cfg = {"join_ack_phrases": ["Ready for you."]}
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild, members=[member])

    result = await runner._handle_discord_auto_voice_join(adapter, member, channel)

    assert result is True
    _, greeting_text = runner._send_voice_reply.await_args.args
    assert greeting_text == "Ready for you."


@pytest.mark.asyncio
async def test_discord_auto_voice_rejoin_after_gateway_restart_uses_configured_ack():
    runner, adapter = _runner_adapter()
    runner._voice_mode["discord:456"] = "voice_only"
    adapter._voice_fx_cfg = {"restart_join_ack_phrases": ["Back online."]}
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild, members=[member])

    result = await runner._handle_discord_auto_voice_join(adapter, member, channel)

    assert result is True
    _, greeting_text = runner._send_voice_reply.await_args.args
    assert greeting_text == "Back online."


@pytest.mark.asyncio
async def test_discord_auto_voice_rejoins_long_session_with_configured_ack():
    runner, adapter = _runner_adapter()
    adapter._voice_fx_cfg = {
        "session_resume_ack_phrases": ["Picking up where we left off."],
        "session_resume_user_turn_threshold": 2,
    }
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(session_id="session-1")
    runner.session_store.load_transcript.return_value = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "one"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild, members=[member])

    result = await runner._handle_discord_auto_voice_join(adapter, member, channel)

    assert result is True
    _, greeting_text = runner._send_voice_reply.await_args.args
    assert greeting_text == "Picking up where we left off."


@pytest.mark.asyncio
async def test_discord_auto_voice_join_skips_greeting_when_already_connected_to_same_channel():
    runner, adapter = _runner_adapter()
    guild = _guild()
    member = _member(member_id=456, guild=guild)
    channel = _channel(guild=guild, members=[member])
    adapter._voice_clients[42] = SimpleNamespace(
        is_connected=MagicMock(return_value=True),
        channel=SimpleNamespace(id=456),
    )

    result = await runner._handle_discord_auto_voice_join(adapter, member, channel)

    assert result is True
    adapter.join_voice_channel.assert_awaited_once_with(channel)
    assert runner._voice_mode["discord:456"] == "voice_only"
    assert adapter._voice_sources[42]["user_id"] == "456"
    runner._send_voice_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_auto_voice_join_uses_busy_ack_when_session_already_running():
    runner, adapter = _runner_adapter()
    adapter._voice_fx_cfg = {"busy_ack_phrases": ["Still working."]}
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild, members=[member])
    runner._running_agents["agent:main:discord:group:456:123"] = object()

    result = await runner._handle_discord_auto_voice_join(adapter, member, channel)

    assert result is True
    runner._send_voice_reply.assert_awaited_once()
    _, greeting_text = runner._send_voice_reply.await_args.args
    assert greeting_text == "Still working."


@pytest.mark.asyncio
async def test_discord_auto_voice_join_waits_for_vc_before_greeting():
    runner, adapter = _runner_adapter()
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild, members=[member])
    adapter.is_in_voice_channel = MagicMock(side_effect=[False, False, True])

    result = await runner._handle_discord_auto_voice_join(adapter, member, channel)

    assert result is True
    assert adapter.is_in_voice_channel.call_count == 3
    runner._send_voice_reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_discord_auto_voice_leave_disables_voice_mode():
    runner, adapter = _runner_adapter()
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild)
    adapter._voice_text_channels[42] = 789
    adapter._auto_voice_session_channels.add("789")
    runner._voice_mode["discord:789"] = "all"
    adapter._auto_tts_enabled_chats.add("789")

    result = await runner._handle_discord_auto_voice_leave(adapter, member, channel)

    assert result is True
    adapter.leave_voice_channel.assert_awaited_once_with(42)
    assert runner._voice_mode["discord:789"] == "off"
    assert adapter._auto_voice_session_channels == set()
    assert "789" in adapter._auto_tts_disabled_chats
    assert "789" not in adapter._auto_tts_enabled_chats
    assert adapter._voice_input_callback is None
    assert adapter._on_voice_disconnect is None


@pytest.mark.asyncio
async def test_discord_auto_voice_leave_preserves_callback_when_other_voice_client_active():
    runner, adapter = _runner_adapter()
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild)
    adapter._voice_text_channels[42] = 789
    adapter._voice_clients[99] = SimpleNamespace()
    adapter._voice_input_callback = runner._handle_voice_channel_input
    adapter._on_voice_disconnect = runner._handle_voice_timeout_cleanup

    result = await runner._handle_discord_auto_voice_leave(adapter, member, channel)

    assert result is True
    assert adapter._voice_input_callback is not None
    assert adapter._on_voice_disconnect is not None


@pytest.mark.asyncio
async def test_discord_auto_voice_join_failure_clears_idle_callbacks():
    runner, adapter = _runner_adapter()
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild)
    adapter.join_voice_channel = AsyncMock(return_value=False)

    result = await runner._handle_discord_auto_voice_join(adapter, member, channel)

    assert result is False
    assert adapter._voice_input_callback is None
    assert adapter._on_voice_disconnect is None


@pytest.mark.asyncio
async def test_discord_auto_voice_join_failure_preserves_callback_when_other_voice_client_active():
    runner, adapter = _runner_adapter()
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild)
    adapter.join_voice_channel = AsyncMock(return_value=False)
    adapter._voice_clients[99] = SimpleNamespace()

    result = await runner._handle_discord_auto_voice_join(adapter, member, channel)

    assert result is False
    assert adapter._voice_input_callback is not None
    assert adapter._on_voice_disconnect is not None

@pytest.mark.asyncio
async def test_adapter_voice_state_join_triggers_runner_only_for_configured_channel():
    guild = _guild()
    member = _member(guild=guild)
    configured = _channel(456, guild=guild, members=[member])
    other = _channel(999, guild=guild, members=[member])
    adapter = DiscordAdapter(PlatformConfig(extra={"auto_voice_channel_id": "456", "auto_voice_user_ids": ["123"]}))
    adapter.gateway_runner = SimpleNamespace(
        _handle_discord_auto_voice_join=AsyncMock(),
        _handle_discord_auto_voice_leave=AsyncMock(),
    )
    adapter._is_allowed_user = MagicMock(return_value=True)

    await adapter._handle_auto_voice_state_update(member, _state(None), _state(other))
    adapter.gateway_runner._handle_discord_auto_voice_join.assert_not_awaited()

    await adapter._handle_auto_voice_state_update(member, _state(None), _state(configured))
    adapter.gateway_runner._handle_discord_auto_voice_join.assert_awaited_once_with(adapter, member, configured)
    adapter.gateway_runner._handle_discord_auto_voice_leave.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_voice_state_leave_waits_until_last_allowed_human_exits():
    guild = _guild()
    member = _member(guild=guild)
    other_member = _member(124, guild=guild)
    channel = _channel(456, guild=guild, members=[other_member])
    adapter = DiscordAdapter(PlatformConfig(extra={"auto_voice_channel_id": "456", "auto_voice_user_ids": ["123", "124"]}))
    adapter.gateway_runner = SimpleNamespace(
        _handle_discord_auto_voice_join=AsyncMock(),
        _handle_discord_auto_voice_leave=AsyncMock(),
    )

    await adapter._handle_auto_voice_state_update(member, _state(channel), _state(None))
    adapter.gateway_runner._handle_discord_auto_voice_leave.assert_not_awaited()

    channel.members = []
    await adapter._handle_auto_voice_state_update(member, _state(channel), _state(None))
    adapter.gateway_runner._handle_discord_auto_voice_leave.assert_awaited_once_with(adapter, member, channel)


@pytest.mark.asyncio
async def test_voice_channel_input_drops_noise_before_transcript_or_agent(caplog):
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace()
    adapter._voice_text_channels = {42: 789}
    adapter._voice_sources = {}
    adapter._auto_voice_session_channels = set()
    adapter._is_auto_voice_user_id_allowed = MagicMock(return_value=False)
    adapter._client = SimpleNamespace(get_channel=MagicMock())
    adapter.handle_message = AsyncMock()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._is_user_authorized = MagicMock(return_value=True)
    runner._recent_voice_transcripts = {}

    caplog.set_level(logging.DEBUG, logger="gateway.voice_mixin")

    await runner._handle_voice_channel_input(
        guild_id=42,
        user_id=123,
        transcript="... --- !!!",
    )

    adapter._client.get_channel.assert_not_called()
    adapter.handle_message.assert_not_awaited()
    assert "Dropping voice transcript before session injection" in caplog.text
    assert "symbol_or_punctuation_junk" in caplog.text


@pytest.mark.asyncio
async def test_voice_channel_input_preserves_short_stop_command():
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace()
    adapter._voice_text_channels = {42: 789}
    adapter._voice_sources = {}
    adapter._auto_voice_session_channels = set()
    adapter._is_auto_voice_user_id_allowed = MagicMock(return_value=False)
    adapter.handle_message = AsyncMock()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._is_user_authorized = MagicMock(return_value=True)
    runner._recent_voice_transcripts = {}

    await runner._handle_voice_channel_input(
        guild_id=42,
        user_id=123,
        transcript="stop",
    )

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "stop"
    assert event.message_type.value == "voice"


def test_adapter_auto_voice_user_ids_restrict_voice_speakers():
    adapter = DiscordAdapter(PlatformConfig(extra={"auto_voice_user_ids": ["123"]}))
    adapter._allowed_user_ids = {"999"}

    assert adapter._is_voice_speaker_allowed(123) is True
    assert adapter._is_voice_speaker_allowed(999) is False


def test_adapter_voice_speaker_falls_back_to_discord_allowlist_without_auto_users():
    adapter = DiscordAdapter(PlatformConfig(extra={}))
    adapter._allowed_user_ids = {"999"}

    assert adapter._is_voice_speaker_allowed(999) is True
    assert adapter._is_voice_speaker_allowed(123) is False


def test_adapter_auto_voice_guild_skips_timeout_for_configured_channel():
    adapter = DiscordAdapter(PlatformConfig(extra={"auto_voice_channel_id": "456", "auto_voice_user_ids": ["123"]}))
    adapter._voice_clients[42] = SimpleNamespace(channel=SimpleNamespace(id=456))
    adapter._voice_timeout_tasks = {}

    adapter._reset_voice_timeout(42)

    assert adapter._voice_timeout_tasks == {}


@pytest.mark.asyncio
async def test_discord_play_tts_suppresses_text_channel_fallback_for_linked_voice_chat(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(extra={}))
    adapter._voice_text_channels[42] = 789
    adapter.is_in_voice_channel = MagicMock(return_value=False)
    adapter.play_in_voice_channel = AsyncMock(return_value=True)
    adapter.send_voice = AsyncMock()
    audio = tmp_path / "reply.ogg"
    audio.write_bytes(b"OggS")

    result = await adapter.play_tts(chat_id="789", audio_path=str(audio))

    assert result.success is False
    adapter.play_in_voice_channel.assert_not_awaited()
    adapter.send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_play_tts_plays_in_vc_for_linked_voice_chat(tmp_path):
    adapter = DiscordAdapter(PlatformConfig(extra={}))
    adapter._voice_text_channels[42] = 789
    adapter.is_in_voice_channel = MagicMock(return_value=True)
    adapter.play_in_voice_channel = AsyncMock(return_value=True)
    adapter.send_voice = AsyncMock()
    audio = tmp_path / "reply.ogg"
    audio.write_bytes(b"OggS")

    result = await adapter.play_tts(chat_id="789", audio_path=str(audio))

    assert result.success is True
    adapter.play_in_voice_channel.assert_awaited_once_with(42, str(audio))
    adapter.send_voice.assert_not_awaited()
