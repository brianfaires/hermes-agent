"""Tests for Discord auto-managed hands-free voice channel presence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
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
    runner._save_voice_modes = MagicMock()

    adapter = SimpleNamespace()
    adapter.join_voice_channel = AsyncMock(return_value=True)
    adapter.leave_voice_channel = AsyncMock()
    adapter._auto_voice_text_channel_id = MagicMock(return_value=789)
    adapter._voice_text_channels = {}
    adapter._voice_transcript_channels = {}
    adapter._auto_voice_session_channels = set()
    adapter._voice_text_suppressed_channels = set()
    adapter._voice_sources = {}
    adapter._auto_tts_enabled_chats = set()
    adapter._auto_tts_disabled_chats = set()
    adapter._voice_input_callback = None
    adapter._on_voice_disconnect = None
    adapter._voice_clients = {}
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
    assert adapter._voice_text_channels[42] == 789
    assert adapter._voice_transcript_channels[42] == 789
    assert adapter._auto_voice_session_channels == {"789"}
    assert adapter._voice_text_suppressed_channels == set()
    assert adapter._voice_sources[42]["platform"] == "discord"
    assert adapter._voice_sources[42]["chat_id"] == "789"
    assert adapter._voice_sources[42]["user_id"] == "123"
    assert runner._voice_mode["discord:789"] == "voice_only"
    runner._save_voice_modes.assert_called_once()
    assert "789" in adapter._auto_tts_enabled_chats
    assert "789" not in adapter._auto_tts_disabled_chats
    assert adapter._voice_input_callback.__func__ is runner._handle_voice_channel_input.__func__
    assert adapter._on_voice_disconnect.__func__ is runner._handle_voice_timeout_cleanup.__func__


@pytest.mark.asyncio
async def test_discord_auto_voice_leave_disables_voice_mode():
    runner, adapter = _runner_adapter()
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild)
    adapter._voice_text_channels[42] = 789
    adapter._voice_transcript_channels[42] = 789
    adapter._auto_voice_session_channels.add("789")
    adapter._voice_text_suppressed_channels.add("789")
    runner._voice_mode["discord:789"] = "all"
    adapter._auto_tts_enabled_chats.add("789")

    result = await runner._handle_discord_auto_voice_leave(adapter, member, channel)

    assert result is True
    adapter.leave_voice_channel.assert_awaited_once_with(42)
    assert runner._voice_mode["discord:789"] == "off"
    assert adapter._auto_voice_session_channels == set()
    assert adapter._voice_text_suppressed_channels == set()
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


def test_adapter_auto_voice_text_channel_requires_explicit_config():
    adapter = DiscordAdapter(
        PlatformConfig(
            extra={"auto_voice_channel_id": "456"},
        )
    )

    assert adapter._auto_voice_text_channel_id() is None


@pytest.mark.asyncio
async def test_discord_auto_voice_join_without_text_channel_still_joins_without_transcript():
    runner, adapter = _runner_adapter()
    adapter._auto_voice_text_channel_id = MagicMock(return_value=None)
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild, members=[member])

    result = await runner._handle_discord_auto_voice_join(adapter, member, channel)

    assert result is True
    adapter.join_voice_channel.assert_awaited_once_with(channel)
    assert adapter._voice_text_channels[42] == 456
    assert adapter._voice_transcript_channels == {}
    assert adapter._auto_voice_session_channels == {"456"}
    assert adapter._voice_text_suppressed_channels == {"456"}
    assert adapter._voice_sources[42]["chat_id"] == "456"
    assert runner._voice_mode["discord:456"] == "voice_only"


@pytest.mark.asyncio
async def test_discord_send_suppresses_text_for_no_transcript_auto_voice_session():
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter._client = None
    adapter._voice_text_suppressed_channels = {"456"}

    result = await adapter.send("456", "spoken reply text")

    assert result.success is True


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
    adapter = DiscordAdapter(PlatformConfig(extra={"auto_voice_text_channel_id": "789"}))
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
    adapter = DiscordAdapter(PlatformConfig(extra={"auto_voice_text_channel_id": "789"}))
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
