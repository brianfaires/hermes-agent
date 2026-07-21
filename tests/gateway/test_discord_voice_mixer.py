"""Tests for the Discord continuous voice mixer (ambient + ducked speech)
and the verbal-ack-before-tool-calls hook.

The mixer (plugins/platforms/discord/voice_mixer.py) is pure-PCM and has no
discord.py dependency, so its core is tested directly.  The adapter
integration (install on join, play routing, ack) is tested with the standard
``object.__new__(DiscordAdapter)`` helper used elsewhere in the voice suite.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# numpy ships only in the optional "voice" extra (not [all,dev]); the mixer
# math needs it, so skip this whole module when it isn't installed.
np = pytest.importorskip("numpy")

# voice_mixer lives inside the discord plugin package dir; import by path the
# same way the adapter does.
_DISCORD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "plugins", "platforms", "discord",
)
if _DISCORD_DIR not in sys.path:
    sys.path.insert(0, _DISCORD_DIR)

import voice_mixer as vm  # noqa: E402


# =====================================================================
# Pure mixer unit tests
# =====================================================================

class TestVoiceMixerCore:
    def test_frame_geometry_matches_discord(self):
        # 20ms @ 48kHz stereo s16 == 3840 bytes (discord.opus.Encoder.FRAME_SIZE)
        assert vm.FRAME_SIZE == 3840
        assert vm.SAMPLES_PER_FRAME == 960
        assert len(vm.SILENCE_FRAME) == vm.FRAME_SIZE

    def test_empty_mixer_returns_silence_frames(self):
        mx = vm.VoiceMixer()
        for _ in range(5):
            frame = mx.read()
            assert len(frame) == vm.FRAME_SIZE
            assert frame == vm.SILENCE_FRAME

    def test_is_opus_false(self):
        # discord.py sends raw PCM when is_opus() is False.
        assert vm.VoiceMixer().is_opus() is False

    def test_ambient_loops_and_is_quiet(self):
        mx = vm.VoiceMixer(ambient_gain=0.2)
        amb = vm.synth_ambient_pcm(seconds=0.5)
        assert len(amb) % vm.FRAME_SIZE == 0  # frame-aligned for seamless loop
        mx.set_ambient(amb)
        peaks = [int(np.max(np.abs(np.frombuffer(mx.read(), dtype=np.int16))))
                 for _ in range(100)]  # 2s >> 0.5s loop
        # Produces audio after the fade-in and stays under the configured gain.
        assert any(p > 0 for p in peaks[10:])
        assert max(peaks) < int(32767 * 0.5)

    def test_speech_audible_over_ambient_then_releases(self):
        mx = vm.VoiceMixer(ambient_gain=0.2, duck_gain=0.05, duck_release_ms=200)
        mx.set_ambient(vm.synth_ambient_pcm(seconds=0.5))
        base = max(int(np.max(np.abs(np.frombuffer(mx.read(), dtype=np.int16))))
                   for _ in range(10))
        tone = (np.sin(2 * np.pi * 440 * np.arange(int(48000 * 0.4)) / 48000)
                * 20000).astype(np.int16)
        stereo = np.repeat(tone[:, None], 2, axis=1).reshape(-1).tobytes()
        mx.play_speech(stereo, fade_in_ms=0)
        assert mx.speech_active
        speech_peak = max(int(np.max(np.abs(np.frombuffer(mx.read(), dtype=np.int16))))
                          for _ in range(15))
        assert speech_peak > base
        # Drain past speech + release ramp; speech_active clears.
        for _ in range(40):
            mx.read()
        assert not mx.speech_active

    def test_clipping_prevents_int16_wraparound(self):
        mx = vm.VoiceMixer()
        loud = (np.ones(vm.SAMPLES_PER_FRAME * 2) * 30000).astype(np.int16).tobytes()
        mx.play_speech(loud, fade_in_ms=0)
        mx.play_speech(loud, fade_in_ms=0)
        out = np.frombuffer(mx.read(), dtype=np.int16)
        assert int(out.max()) == 32767     # clamped, not wrapped to negative
        assert int(out.min()) >= -32768

    def test_stop_speech_clears_in_flight(self):
        mx = vm.VoiceMixer()
        tone = (np.ones(48000) * 10000).astype(np.int16)
        stereo = np.repeat(tone[:, None], 2, axis=1).reshape(-1).tobytes()
        mx.play_speech(stereo)
        assert mx.speech_active
        mx.stop_speech()
        mx.read()
        assert not mx.speech_active

    def test_set_ambient_none_clears(self):
        mx = vm.VoiceMixer()
        mx.set_ambient(vm.synth_ambient_pcm(seconds=0.5))
        mx.set_ambient(None)
        # No ambient, no speech -> silence.
        assert mx.read() == vm.SILENCE_FRAME

    def test_cleanup_silences(self):
        mx = vm.VoiceMixer()
        mx.set_ambient(vm.synth_ambient_pcm(seconds=0.5))
        mx.cleanup()
        assert mx.read() == vm.SILENCE_FRAME

    def test_pcm_not_frame_aligned_is_padded(self):
        # Odd-length PCM must be padded to whole frames (no IndexError, no click).
        mx = vm.VoiceMixer()
        mx.play_speech(b"\x01\x02\x03", fade_in_ms=0)  # 3 bytes << one frame
        out = mx.read()
        assert len(out) == vm.FRAME_SIZE

    def test_synth_ambient_is_stereo_and_frame_aligned(self):
        pcm = vm.synth_ambient_pcm(seconds=1.0)
        assert len(pcm) % (vm.CHANNELS * vm.SAMPLE_WIDTH) == 0
        assert len(pcm) % vm.FRAME_SIZE == 0


# =====================================================================
# Adapter integration
# =====================================================================

def _make_adapter(fx_cfg=None):
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.config import Platform, PlatformConfig
    config = PlatformConfig(enabled=True, extra={})
    config.token = "fake-token"
    adapter = object.__new__(DiscordAdapter)
    adapter.platform = Platform.DISCORD
    adapter.config = config
    adapter._client = MagicMock()
    adapter._voice_clients = {}
    adapter._voice_locks = {}
    adapter._voice_text_channels = {}
    adapter._voice_sources = {}
    adapter._voice_timeout_tasks = {}
    adapter._voice_receivers = {}
    adapter._voice_listen_tasks = {}
    adapter._voice_mixers = {}
    adapter._ambient_pcm_cache = None
    adapter._voice_fx_cfg = fx_cfg if fx_cfg is not None else {
        "enabled": True, "ambient_enabled": True, "ambient_path": "",
        "ambient_gain": 0.18, "duck_gain": 0.06, "speech_gain": 1.0,
        "ack_enabled": True, "ack_phrases": ["One moment."],
    }
    return adapter


class TestInstallVoiceMixer:
    @pytest.mark.asyncio
    async def test_installs_discord_audio_source_wrapper(self, monkeypatch):
        import plugins.platforms.discord.adapter as discord_adapter
        from plugins.platforms.discord.adapter import DiscordAdapter

        class _FakeAudioSource:
            pass

        monkeypatch.setattr(discord_adapter.discord, "AudioSource", _FakeAudioSource, raising=False)
        adapter = _make_adapter()
        adapter._get_ambient_pcm = MagicMock(return_value=None)
        vc = MagicMock()
        vc.is_playing.return_value = False

        def _strict_play(source, *, after=None):
            # Match discord.py VoiceClient.play's contract: it rejects objects
            # that merely look like AudioSource but do not inherit from it.
            if not isinstance(source, _FakeAudioSource):
                raise TypeError(f"source must be an AudioSource not {source.__class__.__name__}")

        vc.play.side_effect = _strict_play

        await DiscordAdapter._install_voice_mixer(adapter, 111, vc)

        source = vc.play.call_args.args[0]
        assert isinstance(source, _FakeAudioSource)
        assert adapter._voice_mixers[111].__class__.__name__ == "VoiceMixer"

    @pytest.mark.asyncio
    async def test_join_does_not_start_continuous_idle_audio(self, monkeypatch):
        """Joining a VC must not transmit an ambient loop while Hermes is idle."""
        import plugins.platforms.discord.adapter as discord_adapter
        from plugins.platforms.discord.adapter import DiscordAdapter

        adapter = _make_adapter()
        adapter._voice_session_generations = {}
        adapter._reset_voice_timeout = MagicMock()
        adapter._schedule_stt_warmup = MagicMock()
        adapter._voice_listen_loop = AsyncMock()

        vc = MagicMock()
        channel = MagicMock()
        channel.guild.id = 111
        channel.connect = AsyncMock(return_value=vc)

        receiver = MagicMock()
        monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
        monkeypatch.setattr(discord_adapter, "VoiceReceiver", MagicMock(return_value=receiver))
        install_mixer = AsyncMock()
        monkeypatch.setattr(adapter, "_install_voice_mixer", install_mixer)

        assert await DiscordAdapter.join_voice_channel(adapter, channel) is True

        install_mixer.assert_not_awaited()
        vc.play.assert_not_called()


class TestVoiceMixerActive:
    def test_false_when_no_mixer(self):
        adapter = _make_adapter()
        assert adapter.voice_mixer_active(111) is False

    def test_true_when_mixer_present(self):
        adapter = _make_adapter()
        adapter._voice_mixers[111] = object()
        assert adapter.voice_mixer_active(111) is True

    def test_false_when_attr_missing(self):
        # Defensive getattr path (object.__new__ helper that forgot the attr).
        from plugins.platforms.discord.adapter import DiscordAdapter
        from gateway.config import Platform
        bare = object.__new__(DiscordAdapter)
        bare.platform = Platform.DISCORD
        assert bare.voice_mixer_active(111) is False


class TestPlayInVoiceChannelMixerPath:
    @pytest.mark.asyncio
    async def test_routes_through_mixer_when_present(self):
        adapter = _make_adapter()
        vc = MagicMock()
        vc.is_connected.return_value = True
        adapter._voice_clients[111] = vc

        # speech_active returns True once (so play_speech is observed) then
        # False so the wait loop exits promptly.
        class _Mixer:
            def __init__(self):
                self._polls = 0
                self.play_speech = MagicMock()

            @property
            def speech_active(self):
                self._polls += 1
                return self._polls <= 1

        mixer = _Mixer()
        adapter._voice_mixers[111] = mixer
        adapter._reset_voice_timeout = MagicMock()

        fake_pcm = b"\x00" * vm.FRAME_SIZE
        with patch.object(vm, "decode_to_pcm", return_value=fake_pcm):
            ok = await adapter.play_in_voice_channel(111, "/tmp/x.mp3")
        assert ok is True
        mixer.play_speech.assert_called_once()
        # Legacy path must NOT have been used.
        vc.play.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_when_decode_fails(self):
        adapter = _make_adapter()
        vc = MagicMock()
        vc.is_connected.return_value = True
        vc.is_playing.return_value = False
        adapter._voice_clients[111] = vc
        adapter._voice_mixers[111] = MagicMock()
        adapter._reset_voice_timeout = MagicMock()
        adapter._voice_receivers[111] = MagicMock()

        with patch.object(vm, "decode_to_pcm", return_value=None), \
                patch("plugins.platforms.discord.adapter.discord") as mock_discord:
            mock_discord.FFmpegPCMAudio.return_value = MagicMock()
            mock_discord.PCMVolumeTransformer.return_value = MagicMock()

            # Make the legacy wait loop resolve immediately without leaving the
            # real Event.wait() coroutine unawaited.
            async def _fast(coro, *a, **k):
                if hasattr(coro, "close"):
                    coro.close()
                return None
            with patch("asyncio.wait_for", _fast):
                ok = await adapter.play_in_voice_channel(111, "/tmp/x.mp3")
        # Fell through to legacy path -> vc.play called.
        assert vc.play.called


class TestStopVoicePlayback:
    @pytest.mark.asyncio
    async def test_stops_mixer_speech_without_tearing_down_continuous_stream(self):
        adapter = _make_adapter()
        mixer = MagicMock()
        vc = MagicMock()
        vc.is_playing.return_value = True
        adapter._voice_mixers[111] = mixer
        adapter._voice_clients[111] = vc

        stopped = await adapter.stop_voice_playback(111)

        assert stopped is True
        mixer.stop_speech.assert_called_once_with()
        vc.stop.assert_not_called()

    @pytest.mark.asyncio
    async def test_stops_legacy_one_shot_playback(self):
        adapter = _make_adapter()
        vc = MagicMock()
        vc.is_playing.return_value = True
        adapter._voice_clients[111] = vc

        stopped = await adapter.stop_voice_playback(111)

        assert stopped is True
        vc.stop.assert_called_once_with()


class TestPlayAckInVoice:
    def test_loads_cancellation_ack_phrases_from_config_yaml(self, tmp_path, monkeypatch):
        from plugins.platforms.discord.adapter import DiscordAdapter

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "discord:\n  voice_fx:\n    cancellation_ack_phrases: [Got it.]\n"
            "    model_switch_ack_phrases: [Changed over.]\n"
            "    join_ack_phrases: [Ready when you are.]\n"
            "    busy_ack_phrases: [Still working.]\n"
            "    restart_join_ack_phrases: [Back online.]\n"
            "    session_resume_ack_phrases: [Picking up.]\n"
            "    session_resume_user_turn_threshold: 3\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = DiscordAdapter._load_voice_fx_config(object.__new__(DiscordAdapter))

        assert config["cancellation_ack_phrases"] == ["Got it."]
        assert config["model_switch_ack_phrases"] == ["Changed over."]
        assert config["join_ack_phrases"] == ["Ready when you are."]
        assert config["busy_ack_phrases"] == ["Still working."]
        assert config["restart_join_ack_phrases"] == ["Back online."]
        assert config["session_resume_ack_phrases"] == ["Picking up."]
        assert config["session_resume_user_turn_threshold"] == 3

    @pytest.mark.asyncio
    async def test_noop_when_ack_disabled(self):
        adapter = _make_adapter({"ack_enabled": False})
        adapter._voice_mixers[111] = MagicMock()
        assert await adapter.play_ack_in_voice(111) is False

    @pytest.mark.asyncio
    async def test_noop_when_no_mixer(self):
        adapter = _make_adapter()
        assert await adapter.play_ack_in_voice(111) is False

    @pytest.mark.asyncio
    async def test_plays_speech_when_armed(self, tmp_path):
        adapter = _make_adapter()
        mixer = MagicMock()
        adapter._voice_mixers[111] = mixer
        adapter._reset_voice_timeout = MagicMock()

        ack_file = tmp_path / "ack.mp3"
        ack_file.write_bytes(b"id3")
        import json as _json
        with patch("tools.tts_tool.text_to_speech_tool",
                   return_value=_json.dumps({"success": True, "file_path": str(ack_file)})), \
                patch.object(vm, "decode_to_pcm", return_value=b"\x00" * vm.FRAME_SIZE):
            ok = await adapter.play_ack_in_voice(111, phrase="Testing one two.")
        assert ok is True
        mixer.play_speech.assert_called_once()

    @pytest.mark.asyncio
    async def test_ack_temp_path_is_profile_scoped(self, tmp_path, monkeypatch):
        adapter = _make_adapter()
        adapter._voice_mixers[111] = MagicMock()
        adapter._reset_voice_timeout = MagicMock()

        ack_file = tmp_path / "ack.mp3"
        ack_file.write_bytes(b"id3")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes" / "profiles" / "ops"))
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path / "tmp"))

        seen = {}
        import json as _json

        def _tts(**kwargs):
            seen["output_path"] = kwargs["output_path"]
            return _json.dumps({"success": True, "file_path": str(ack_file)})

        with patch("tools.tts_tool.text_to_speech_tool", side_effect=_tts), \
                patch.object(vm, "decode_to_pcm", return_value=b"\x00" * vm.FRAME_SIZE):
            ok = await adapter.play_ack_in_voice(111, phrase="Testing one two.")

        assert ok is True
        assert os.path.dirname(seen["output_path"]) == str(tmp_path / "tmp" / "hermes_voice" / "ops")
        assert os.path.basename(seen["output_path"]).startswith("ack_")

    @pytest.mark.asyncio
    async def test_cancellation_ack_uses_configured_phrase_list(self, tmp_path):
        adapter = _make_adapter({"cancellation_ack_phrases": ["Sure thing.", "Ignored."]})
        adapter._voice_clients[111] = MagicMock()
        adapter._voice_clients[111].is_connected.return_value = True
        adapter.play_in_voice_channel = AsyncMock(return_value=True)
        ack_file = tmp_path / "cancellation.mp3"
        ack_file.write_bytes(b"id3")
        seen = {}
        import json as _json

        def _tts(**kwargs):
            seen["text"] = kwargs["text"]
            return _json.dumps({"success": True, "file_path": str(ack_file)})

        with patch("tools.tts_tool.text_to_speech_tool", side_effect=_tts), \
                patch("random.choice", return_value="Ignored."):
            ok = await adapter.play_cancellation_ack_in_voice(111)

        assert ok is True
        assert seen["text"] == "Ignored."
        adapter.play_in_voice_channel.assert_awaited_once_with(111, str(ack_file))
