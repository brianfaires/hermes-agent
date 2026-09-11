"""Focused Discord voice restoration regressions."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(
        View=object,
        button=lambda *a, **k: (lambda fn: fn),
        Button=object,
    )
    discord_mod.ButtonStyle = SimpleNamespace(
        success=1,
        primary=2,
        secondary=2,
        danger=3,
        green=1,
        grey=2,
        blurple=2,
        red=3,
    )
    discord_mod.Color = SimpleNamespace(
        orange=lambda: 1,
        green=lambda: 2,
        blue=lambda: 3,
        red=lambda: 4,
        purple=lambda: 5,
    )
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    discord_mod.opus = SimpleNamespace(
        is_loaded=lambda: True,
        load_opus=lambda *_args, **_kwargs: None,
    )
    discord_mod.FFmpegPCMAudio = MagicMock
    discord_mod.PCMVolumeTransformer = MagicMock
    discord_mod.http = SimpleNamespace(Route=MagicMock)
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from gateway.config import Platform, PlatformConfig, load_gateway_config
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.run import DiscordVoiceProgressSpeaker, GatewayRunner, TurnRunner
from gateway.session import SessionSource, build_session_key
from gateway.turn_context import TurnContext
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from plugins.platforms.discord.adapter import DiscordAdapter, _load_profile_stt_aliases


def _bare_runner(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._voice_mode = {}
    runner._VOICE_MODE_PATH = tmp_path / "gateway_voice_mode.json"
    runner._recent_voice_transcripts = {}
    runner._save_voice_modes = MagicMock()
    runner._is_user_authorized = MagicMock(return_value=True)
    runner._should_echo_stt_transcripts = MagicMock(return_value=True)
    runner._send_voice_reply = AsyncMock()
    return runner


def _dispatcher_runner(tmp_path, adapter):
    from datetime import datetime

    from gateway.config import GatewayConfig
    from gateway.session import SessionEntry

    runner = _bare_runner(tmp_path)
    runner.config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                token="***",
                extra={},
            )
        }
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store._generate_session_key.side_effect = (
        lambda source: build_session_key(
            source, profile=source.profile,
            group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
        )
    )
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:ops:discord:group:789",
        session_id="sess-voice-alias",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.DISCORD,
        chat_type="group",
        total_tokens=0,
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_sources = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._session_db.get_session.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._set_session_env = lambda _context: None
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._profile_adapters = {"ops": {Platform.DISCORD: adapter}}
    return runner


def _discord_adapter_for_voice_dispatch():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="token"))
    adapter._runtime_profile_name = "ops"
    adapter._running = True
    adapter._voice_text_channels[42] = 789
    adapter._voice_sources[42] = SessionSource(
        platform=Platform.DISCORD,
        chat_id="789",
        chat_name="Commute",
        chat_type="group",
        user_id="old",
        user_name="Old",
        guild_id="42",
        profile="ops",
    ).to_dict()
    adapter._client = SimpleNamespace(get_channel=MagicMock(return_value=None))
    adapter._resolve_channel_prompt = MagicMock(return_value="channel prompt")
    adapter._is_allowed_user = MagicMock(return_value=True)
    sent = []

    async def _send(chat_id, content, reply_to=None, metadata=None):
        sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id=f"sent-{len(sent)}")

    adapter.send = _send
    adapter.send_typing = AsyncMock()
    adapter.stop_typing = AsyncMock()
    adapter._captured_sends = sent
    return adapter


def _adapter(*, profile=None, auto_text_channel_id=None):
    adapter = SimpleNamespace()
    adapter.platform = Platform.DISCORD
    adapter.config = SimpleNamespace(extra={})
    if auto_text_channel_id is not None:
        adapter.config.extra["auto_voice_text_channel_id"] = auto_text_channel_id
    adapter.join_voice_channel = AsyncMock(return_value=True)
    adapter.leave_voice_channel = AsyncMock()
    adapter.is_in_voice_channel = MagicMock(return_value=True)
    adapter.stop_voice_playback = AsyncMock(return_value=True)
    adapter.handle_message = AsyncMock()
    adapter._voice_text_channels = {}
    adapter._auto_voice_session_channels = set()
    adapter._voice_sources = {}
    adapter._voice_clients = {}
    adapter._auto_tts_enabled_chats = set()
    adapter._auto_tts_disabled_chats = set()
    adapter._voice_input_callback = None
    adapter._on_voice_disconnect = None
    adapter._voice_mode_getter = None
    adapter._runtime_profile_name = profile
    adapter._client = SimpleNamespace(get_channel=MagicMock(return_value=AsyncMock()))
    adapter._resolve_channel_prompt = MagicMock(return_value="channel prompt")
    return adapter


def _guild(guild_id=42):
    return SimpleNamespace(id=guild_id)


def _member(member_id=123, guild=None):
    return SimpleNamespace(
        id=member_id,
        display_name="Brian",
        guild=guild or _guild(),
        bot=False,
    )


def _channel(channel_id=456, guild=None, members=None):
    return SimpleNamespace(
        id=channel_id,
        name="Commute",
        guild=guild or _guild(),
        members=members if members is not None else [],
    )


@pytest.mark.asyncio
async def test_auto_voice_join_stamps_profile_and_uses_configured_text_channel(tmp_path):
    runner = _bare_runner(tmp_path)
    adapter = _adapter(profile="ops", auto_text_channel_id=789)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {"ops": {Platform.DISCORD: adapter}}
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild, members=[member])

    assert await runner._handle_discord_auto_voice_join(adapter, member, channel)

    adapter.join_voice_channel.assert_awaited_once_with(channel)
    assert adapter._voice_text_channels[42] == 789
    assert adapter._auto_voice_session_channels == {"789"}
    assert adapter._voice_sources[42]["profile"] == "ops"
    assert runner._voice_mode["discord:789"] == "voice_only"
    event, text = runner._send_voice_reply.await_args.args
    assert event.source.chat_id == "789"
    assert event.source.profile == "ops"
    assert text == "I'm here. What's up?"


@pytest.mark.asyncio
async def test_manual_voice_leave_suppresses_configured_auto_rejoin(tmp_path):
    runner = _bare_runner(tmp_path)
    adapter = _adapter(profile="ops", auto_text_channel_id=789)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._profile_adapters = {"ops": {Platform.DISCORD: adapter}}
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild, members=[member])
    event = MessageEvent(
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="789",
            chat_type="group",
            user_id="123",
            profile="ops",
        ),
        text="/voice leave",
        message_type=MessageType.TEXT,
        raw_message=SimpleNamespace(guild_id=42, guild=guild),
    )

    await runner._handle_voice_channel_leave(event)
    assert runner._voice_mode["discord:789"] == "off"

    assert await runner._handle_discord_auto_voice_join(adapter, member, channel) is False
    adapter.join_voice_channel.assert_not_awaited()
    assert runner._voice_mode["discord:789"] == "off"


@pytest.mark.asyncio
async def test_auto_departure_does_not_persist_manual_rejoin_suppression(tmp_path):
    runner = _bare_runner(tmp_path)
    adapter = _adapter(profile="ops", auto_text_channel_id=789)
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild, members=[member])
    runner._voice_mode["discord:789"] = "voice_only"
    adapter._voice_text_channels[42] = 789
    adapter._auto_voice_session_channels.add("789")

    assert await runner._handle_discord_auto_voice_leave(adapter, member, channel)
    assert "discord:789" not in runner._voice_mode
    assert adapter._auto_voice_session_channels == set()

    runner._send_voice_reply.reset_mock()
    adapter.join_voice_channel.reset_mock()
    assert await runner._handle_discord_auto_voice_join(adapter, member, channel)
    adapter.join_voice_channel.assert_awaited_once_with(channel)
    assert runner._voice_mode["discord:789"] == "voice_only"


@pytest.mark.asyncio
async def test_auto_departure_preserves_voice_off_suppression(tmp_path):
    runner = _bare_runner(tmp_path)
    adapter = _adapter(profile="ops", auto_text_channel_id=789)
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild, members=[member])
    runner._voice_mode["discord:789"] = "off"
    adapter._voice_text_channels[42] = 789
    adapter._auto_voice_session_channels.add("789")

    assert await runner._handle_discord_auto_voice_leave(adapter, member, channel)
    assert runner._voice_mode["discord:789"] == "off"
    assert adapter._auto_voice_session_channels == set()

    adapter.join_voice_channel.reset_mock()
    assert await runner._handle_discord_auto_voice_join(adapter, member, channel) is False
    adapter.join_voice_channel.assert_not_awaited()
    assert runner._voice_mode["discord:789"] == "off"


@pytest.mark.asyncio
async def test_voice_input_never_bypasses_runner_authorization(tmp_path):
    runner = _bare_runner(tmp_path)
    runner._is_user_authorized.return_value = False
    adapter = _adapter(profile="ops", auto_text_channel_id=789)
    adapter._voice_text_channels[42] = 789
    adapter._auto_voice_session_channels.add("789")
    adapter._voice_sources[42] = SessionSource(
        platform=Platform.DISCORD,
        chat_id="789",
        chat_type="group",
        user_id="123",
        profile="ops",
    ).to_dict()

    await runner._handle_voice_channel_input(
        42,
        123,
        "status please",
        adapter=adapter,
    )

    runner._is_user_authorized.assert_called_once()
    adapter.handle_message.assert_not_awaited()
    adapter._client.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_voice_input_transcript_echo_requires_auto_text_channel_opt_in(tmp_path):
    runner = _bare_runner(tmp_path)
    adapter = _adapter(profile="ops", auto_text_channel_id=None)
    adapter._voice_text_channels[42] = 789
    adapter._voice_sources[42] = SessionSource(
        platform=Platform.DISCORD,
        chat_id="789",
        chat_type="group",
        user_id="123",
        profile="ops",
    ).to_dict()

    await runner._handle_voice_channel_input(
        42,
        123,
        "yes, keep going",
        adapter=adapter,
    )

    adapter._client.get_channel.assert_not_called()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "yes, keep going"
    assert event.channel_prompt == "channel prompt"
    assert event.source.profile == "ops"


def test_voice_duplicate_transcripts_are_profile_scoped(tmp_path):
    runner = _bare_runner(tmp_path)

    assert not runner._is_duplicate_voice_transcript(
        42,
        123,
        "same words",
        profile="ops",
    )
    assert not runner._is_duplicate_voice_transcript(
        42,
        123,
        "same words",
        profile="default",
    )
    assert runner._is_duplicate_voice_transcript(
        42,
        123,
        "same words",
        profile="ops",
    )


def test_auto_voice_member_requires_config_and_existing_authorization():
    adapter = DiscordAdapter(
        PlatformConfig(
            enabled=True,
            token="token",
            extra={"auto_voice_user_ids": ["123"]},
        )
    )
    adapter._is_allowed_user = MagicMock(return_value=False)

    assert adapter._is_auto_voice_member_allowed(_member(123)) is False
    adapter._is_allowed_user.return_value = True
    assert adapter._is_auto_voice_member_allowed(_member(123)) is True
    assert adapter._is_auto_voice_member_allowed(_member(999)) is False


@pytest.mark.asyncio
async def test_auto_voice_presence_respects_channel_allowlist():
    adapter = DiscordAdapter(
        PlatformConfig(
            enabled=True,
            token="token",
            extra={
                "auto_voice_channel_id": "456",
                "auto_voice_text_channel_id": "789",
                "allowed_channels": "999",
            },
        )
    )
    guild = _guild()
    member = _member(guild=guild)
    channel = _channel(guild=guild, members=[member])
    adapter._client = SimpleNamespace(get_channel=MagicMock(return_value=channel))
    adapter.gateway_runner = SimpleNamespace(_handle_discord_auto_voice_join=AsyncMock())
    adapter._is_allowed_user = MagicMock(return_value=True)

    await adapter._sync_auto_voice_presence()

    adapter.gateway_runner._handle_discord_auto_voice_join.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_voice_input_applies_stt_alias_before_callback():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="token"))
    adapter._stt_aliases = {"/queue continue": ["keep going"]}
    callback = AsyncMock()
    adapter._voice_input_callback = callback

    with patch("plugins.platforms.discord.adapter.VoiceReceiver.pcm_to_wav"), \
         patch(
             "tools.transcription_tools.transcribe_audio",
             return_value={"success": True, "transcript": "keep going"},
         ), \
         patch("tools.voice_mode.is_whisper_hallucination", return_value=False):
        await adapter._process_voice_input(42, 123, b"\x00" * 96000)

    callback.assert_awaited_once_with(
        guild_id=42,
        user_id=123,
        transcript="/queue continue",
    )


def test_voice_ack_callback_uses_secondary_profile_adapter(tmp_path):
    runner = _bare_runner(tmp_path)
    primary = _adapter()
    secondary = _adapter(profile="ops")
    primary.play_ack_in_voice = AsyncMock(return_value=True)
    secondary.play_ack_in_voice = AsyncMock(return_value=True)
    runner.adapters = {Platform.DISCORD: primary}
    runner._profile_adapters = {"ops": {Platform.DISCORD: secondary}}
    source = SessionSource(platform=Platform.DISCORD, chat_id="789", profile="ops")
    loop = asyncio.new_event_loop()
    try:
        ctx = TurnContext(
            source=source,
            _run_still_current=lambda: True,
            _voice_ack_fired=[False],
            _voice_ack_guild=[42],
            _voice_ack_loop=loop,
        )
        turn_runner = TurnRunner(runner, ctx)
        turn_runner.voice_ack_callback("call-1", "execute_code", {})
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()

    primary.play_ack_in_voice.assert_not_called()
    secondary.play_ack_in_voice.assert_called_once_with(42)


def test_stt_alias_rewrite_preserves_unknown_text_and_arguments():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="token"))
    adapter._stt_aliases = {
        "/queue": ["queue"],
        "/queue continue": ["keep going"],
        "/model": ["switch model"],
        "/model gpt-5": ["load gpt five"],
    }

    assert adapter._rewrite_stt_alias("keep going") == "/queue continue"
    assert adapter._rewrite_stt_alias("queue Continue this, please!") == (
        "/queue Continue this, please!"
    )
    assert adapter._rewrite_stt_alias("switch model GPT-5, now") == (
        "/model GPT-5, now"
    )
    assert adapter._rewrite_stt_alias("Yeah, load um gpt five") == "/model gpt-5"
    assert adapter._rewrite_stt_alias("please keep going") == "please keep going"


@pytest.mark.asyncio
async def test_voice_alias_idle_dispatches_steer_tail_through_normal_handler(tmp_path):
    adapter = _discord_adapter_for_voice_dispatch()
    adapter._stt_aliases = {"/steer": ["steer"]}
    runner = _dispatcher_runner(tmp_path, adapter)
    runner._handle_message_with_agent = AsyncMock(return_value="agent accepted")
    runner._wire_discord_voice_callbacks(adapter)
    adapter.set_message_handler(runner._handle_message)

    with patch("plugins.platforms.discord.adapter.VoiceReceiver.pcm_to_wav"), \
         patch(
             "tools.transcription_tools.transcribe_audio",
             return_value={"success": True, "transcript": "steer Keep CASE, please!"},
         ), \
         patch("tools.voice_mode.is_whisper_hallucination", return_value=False):
        await adapter._process_voice_input(42, 123, b"\x00" * 96000)

    await asyncio.gather(*list(adapter._background_tasks))

    assert any(stage == "stt_ready" for stage, _ in adapter.voice_timing().snapshot())
    assert runner._is_user_authorized.call_count >= 1
    event = runner._handle_message_with_agent.await_args.args[0]
    assert event.text == "Keep CASE, please!"
    assert event.source.user_id == "123"
    assert event.source.profile == "ops"
    assert event.channel_prompt == "channel prompt"


@pytest.mark.asyncio
async def test_voice_alias_active_queue_uses_normal_busy_dispatcher(tmp_path):
    adapter = _discord_adapter_for_voice_dispatch()
    adapter._stt_aliases = {"/queue": ["queue"]}
    runner = _dispatcher_runner(tmp_path, adapter)
    runner._wire_discord_voice_callbacks(adapter)
    adapter.set_message_handler(runner._handle_message)
    source = SessionSource.from_dict(adapter._voice_sources[42])
    source.user_id = "123"
    source.user_name = "123"
    session_key = build_session_key(
        source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
        profile=adapter._session_key_profile(source),
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    runner._running_agents[session_key] = MagicMock()

    with patch("plugins.platforms.discord.adapter.VoiceReceiver.pcm_to_wav"), \
         patch(
             "tools.transcription_tools.transcribe_audio",
             return_value={
                 "success": True,
                 "transcript": "queue Continue this after the current turn.",
             },
         ), \
         patch("tools.voice_mode.is_whisper_hallucination", return_value=False):
        await adapter._process_voice_input(42, 123, b"\x00" * 96000)

    assert adapter._captured_sends[-1]["content"] == "Queued for the next turn."
    queued = adapter._pending_messages[session_key]
    assert queued.text == "Continue this after the current turn."
    assert queued.source.user_id == "123"
    assert queued.source.profile == "ops"
    assert queued.channel_prompt == "channel prompt"


def test_gateway_config_loads_auto_voice_keys_into_discord_extra(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "\n".join(
            [
                "discord:",
                "  enabled: true",
                "  token: token",
                "  auto_voice_channel_id: '456'",
                "  auto_voice_text_channel_id: '789'",
                "  auto_voice_user_ids:",
                "    - '123'",
            ]
        ),
        encoding="utf-8",
    )

    token = set_hermes_home_override(str(tmp_path))
    try:
        config = load_gateway_config()
    finally:
        reset_hermes_home_override(token)

    extra = config.platforms[Platform.DISCORD].extra
    assert extra["auto_voice_channel_id"] == "456"
    assert extra["auto_voice_text_channel_id"] == "789"
    assert extra["auto_voice_user_ids"] == ["123"]


def test_stt_alias_catalog_missing_or_malformed_fails_closed(tmp_path):
    token = set_hermes_home_override(str(tmp_path))
    try:
        assert _load_profile_stt_aliases() == {}
        voice_dir = tmp_path / "voice"
        voice_dir.mkdir()
        (voice_dir / "commands.toml").write_bytes(b"\xff\xfe\x00")
        assert _load_profile_stt_aliases() == {}
        (voice_dir / "commands.toml").write_text("not valid = [", encoding="utf-8")
        assert _load_profile_stt_aliases() == {}
    finally:
        reset_hermes_home_override(token)


def test_stt_alias_catalog_is_profile_local(tmp_path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    (one / "voice").mkdir(parents=True)
    (two / "voice").mkdir(parents=True)
    (one / "voice" / "commands.toml").write_text(
        "[stt_aliases]\n'/queue continue' = ['keep going']\n",
        encoding="utf-8",
    )
    (two / "voice" / "commands.toml").write_text(
        "[stt_aliases]\n'/model gpt-5' = ['load gpt five']\n",
        encoding="utf-8",
    )

    token = set_hermes_home_override(str(one))
    try:
        assert _load_profile_stt_aliases() == {"/queue continue": ["keep going"]}
    finally:
        reset_hermes_home_override(token)

    token = set_hermes_home_override(str(two))
    try:
        assert _load_profile_stt_aliases() == {"/model gpt-5": ["load gpt five"]}
    finally:
        reset_hermes_home_override(token)


def test_voice_progress_lifecycle_callbacks_accept_agent_completion_shape():
    speaker = SimpleNamespace(tool_started=MagicMock(), tool_completed=MagicMock())
    ctx = TurnContext(
        source=SessionSource(platform=Platform.DISCORD, chat_id="789"),
        _run_still_current=lambda: True,
        voice_progress_speaker=speaker,
    )
    turn_runner = TurnRunner(SimpleNamespace(), ctx)

    turn_runner.combined_tool_start_callback("call-1", "execute_code", {"cmd": "date"})
    turn_runner.combined_tool_complete_callback(
        "call-1",
        "execute_code",
        {"cmd": "date"},
        "ok",
    )

    speaker.tool_started.assert_called_once_with(
        "call-1",
        "execute_code",
        {"cmd": "date"},
    )
    speaker.tool_completed.assert_called_once_with(
        "call-1",
        "execute_code",
        {"cmd": "date"},
        "ok",
    )


@pytest.mark.asyncio
async def test_voice_progress_speaks_after_silence_for_active_voice_tool():
    adapter = SimpleNamespace(is_in_voice_channel=MagicMock(return_value=True))
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="789",
        guild_id="42",
        profile="ops",
    )
    event = MessageEvent(
        source=source,
        text="look this up",
        message_type=MessageType.VOICE,
        raw_message=SimpleNamespace(guild_id=42),
    )
    runner = SimpleNamespace(
        _adapter_for_source=MagicMock(return_value=adapter),
        _send_voice_reply=AsyncMock(),
    )
    speaker = DiscordVoiceProgressSpeaker(
        runner,
        event,
        loop=asyncio.get_running_loop(),
        guild_id=42,
        silence_seconds=0.01,
    )

    speaker.tool_started("call-1", "execute_code", {"cmd": "date"})
    await asyncio.sleep(0.04)
    await speaker.close()

    runner._send_voice_reply.assert_awaited()
    spoken_event, text = runner._send_voice_reply.await_args.args
    assert spoken_event.source.profile == "ops"
    assert text == "Still working on execute code."


@pytest.mark.asyncio
async def test_voice_progress_completion_cancels_pending_tool_speech():
    adapter = SimpleNamespace(is_in_voice_channel=MagicMock(return_value=True))
    event = MessageEvent(
        source=SessionSource(platform=Platform.DISCORD, chat_id="789", guild_id="42"),
        text="check",
        message_type=MessageType.VOICE,
        raw_message=SimpleNamespace(guild_id=42),
    )
    runner = SimpleNamespace(
        _adapter_for_source=MagicMock(return_value=adapter),
        _send_voice_reply=AsyncMock(),
    )
    speaker = DiscordVoiceProgressSpeaker(
        runner,
        event,
        loop=asyncio.get_running_loop(),
        guild_id=42,
        silence_seconds=0.02,
    )

    speaker.tool_started("call-1", "web_search", {})
    speaker.tool_completed("call-1", "web_search", {}, "done")
    await asyncio.sleep(0)
    await speaker.close()

    runner._send_voice_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_progress_stale_or_disconnected_turn_does_not_speak():
    connected = True
    current = True

    def _connected(_guild_id):
        return connected

    adapter = SimpleNamespace(is_in_voice_channel=MagicMock(side_effect=_connected))
    event = MessageEvent(
        source=SessionSource(platform=Platform.DISCORD, chat_id="789", guild_id="42"),
        text="check",
        message_type=MessageType.VOICE,
        raw_message=SimpleNamespace(guild_id=42),
    )
    setattr(event, "_voice_progress_run_still_current", lambda: current)
    runner = SimpleNamespace(
        _adapter_for_source=MagicMock(return_value=adapter),
        _send_voice_reply=AsyncMock(),
    )
    speaker = DiscordVoiceProgressSpeaker(
        runner,
        event,
        loop=asyncio.get_running_loop(),
        guild_id=42,
        silence_seconds=0.01,
    )

    speaker.tool_started("call-1", "web_search", {})
    current = False
    await asyncio.sleep(0.03)
    runner._send_voice_reply.assert_not_awaited()

    current = True
    speaker.tool_started("call-2", "web_search", {})
    connected = False
    await asyncio.sleep(0.03)
    await speaker.close()

    runner._send_voice_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_progress_mute_during_long_tool_prevents_queued_speech():
    allowed = True
    adapter = SimpleNamespace(is_in_voice_channel=MagicMock(return_value=True))
    event = MessageEvent(
        source=SessionSource(platform=Platform.DISCORD, chat_id="789", guild_id="42"),
        text="check",
        message_type=MessageType.VOICE,
        raw_message=SimpleNamespace(guild_id=42),
    )

    def _allowed(_event, _text):
        return allowed

    runner = SimpleNamespace(
        _adapter_for_source=MagicMock(return_value=adapter),
        _should_send_voice_progress_reply=MagicMock(side_effect=_allowed),
        _send_voice_reply=AsyncMock(),
    )
    speaker = DiscordVoiceProgressSpeaker(
        runner,
        event,
        loop=asyncio.get_running_loop(),
        guild_id=42,
        silence_seconds=0.01,
    )

    speaker.tool_started("call-1", "web_search", {})
    allowed = False
    await asyncio.sleep(0.04)
    await speaker.close()

    runner._send_voice_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_progress_close_cancels_pending_commentary_speech():
    started = asyncio.Event()
    adapter = SimpleNamespace(is_in_voice_channel=MagicMock(return_value=True))
    event = MessageEvent(
        source=SessionSource(platform=Platform.DISCORD, chat_id="789", guild_id="42"),
        text="check",
        message_type=MessageType.VOICE,
        raw_message=SimpleNamespace(guild_id=42),
    )

    async def _slow_send(_event, _text):
        started.set()
        await asyncio.Event().wait()

    runner = SimpleNamespace(
        _adapter_for_source=MagicMock(return_value=adapter),
        _should_send_voice_progress_reply=MagicMock(return_value=True),
        _send_voice_reply=AsyncMock(side_effect=_slow_send),
    )
    speaker = DiscordVoiceProgressSpeaker(
        runner,
        event,
        loop=asyncio.get_running_loop(),
        guild_id=42,
        silence_seconds=10.0,
    )

    speaker.speak_commentary("I'll inspect the repo first.", wait_timeout=0)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await speaker.close()

    assert speaker.spoken_text_keys() == set()
    assert runner._send_voice_reply.await_count == 1
