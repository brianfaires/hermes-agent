import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter, VoiceReceiver
from plugins.platforms.discord.voice_output import output_scope


def adapter():
    obj = DiscordAdapter(PlatformConfig(enabled=True, token="fixture"))
    obj._client = SimpleNamespace(get_guild=lambda gid: SimpleNamespace(id=gid))
    obj._voice_session_generations[42] = 1
    obj._voice_clients[42] = MagicMock()
    obj._voice_clients[42].is_connected.return_value = True
    obj._voice_clients[42].is_playing.return_value = True
    obj._voice_clients[42].stop.side_effect = lambda: setattr(obj._voice_clients[42].is_playing, "return_value", False)
    obj._is_voice_speaker_allowed = MagicMock(return_value=True)
    obj._reset_voice_timeout = MagicMock()
    return obj


@pytest.mark.asyncio
async def test_confirmed_mapped_pcm_stops_before_stt_and_denials_do_not_stop():
    obj = adapter()
    receiver = VoiceReceiver(obj._voice_clients[42], on_speech_onset=lambda uid: obj._schedule_voice_onset(42, uid, 1))
    receiver.map_ssrc(10, 123)
    receiver._confirm_speech_onset(10, b'\0' * 19200)
    assert not obj._background_tasks
    receiver._confirm_speech_onset(99, struct.pack('<h', 500) * 9600)
    assert not obj._background_tasks  # unmapped SSRC never inferred for onset
    receiver._confirm_speech_onset(10, struct.pack('<h', 500) * 9600)
    await asyncio.gather(*obj._background_tasks)
    obj._voice_clients[42].stop.assert_called_once()
    assert obj._voice_output_generation(42) == 1
    obj._is_voice_speaker_allowed.return_value = False
    obj._schedule_voice_onset(42, 456, 1)
    obj._schedule_voice_onset(42, 123, 0)  # stale receiver after reconnect
    assert obj._voice_output_generation(42) == 1


@pytest.mark.asyncio
async def test_stop_invalidates_late_decode_and_old_turn_after_new_input():
    obj = adapter()
    entered, release = asyncio.Event(), asyncio.Event()
    async def probe(_):
        entered.set()
        await release.wait()
        return 30
    obj._playback_timeout_for_audio = probe
    token = output_scope.set((obj, 42, 0))
    try:
        task = asyncio.create_task(obj.play_in_voice_channel(42, 'fixture.wav'))
        await entered.wait()
        await obj.stop_voice_playback(42)
        release.set()
        assert await task is False
        assert not obj.voice_output_current(42)
        assert await obj.play_in_voice_channel(42, 'late.wav') is False
        obj._voice_clients[42].play.assert_not_called()
    finally:
        output_scope.reset(token)
    assert obj.voice_output_current(42)


@pytest.mark.asyncio
async def test_other_adapter_generation_is_not_affected():
    first, second = adapter(), adapter()
    token = output_scope.set((second, 42, 0))
    try:
        await first.stop_voice_playback(42)
        assert second.voice_output_current(42)
    finally:
        output_scope.reset(token)


@pytest.mark.asyncio
async def test_stt_older_than_new_onset_is_not_dispatched(monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    obj = adapter()
    obj._voice_input_callback = AsyncMock()
    def stt(_):
        entered.set()
        release.wait(3)
        return {'success': True, 'transcript': 'Old request that must never speak.'}
    monkeypatch.setattr(VoiceReceiver, 'pcm_to_wav', lambda *args: None)
    monkeypatch.setattr('tools.transcription_tools.transcribe_audio', stt)
    task = asyncio.create_task(obj._process_voice_input(42, 123, b'\0' * 96000, session_generation=1))
    assert await asyncio.to_thread(entered.wait, 2)
    await obj.stop_voice_playback(42)
    release.set()
    await asyncio.wait_for(task, 3)
    obj._voice_input_callback.assert_not_awaited()
