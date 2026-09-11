import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from gateway.config import PlatformConfig
from gateway.platforms.base import AudioFormat
from gateway.streaming_tts_consumer import StreamingTTSConsumer
from plugins.platforms.discord.adapter import DiscordAdapter
from plugins.platforms.discord.voice_stream import PCMStream, FRAME_BYTES


def make_adapter():
    obj = DiscordAdapter(PlatformConfig(enabled=True, token="fixture"))
    obj._voice_text_channels[42] = 789
    obj._cancel_voice_timeout = MagicMock()
    obj._reset_voice_timeout = MagicMock()
    vc = MagicMock()
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    obj._voice_clients[42] = vc
    return obj


@pytest.mark.asyncio
async def test_pcm_alignment_conversion_eof_and_bounded_cancel():
    source = PCMStream(AudioFormat())
    pcm = struct.pack('<h', 500) * 2400
    await source.write(pcm[:1])
    await source.write(pcm[1:])
    await source.finish()
    frames = []
    while frame := source.read():
        frames.append(frame)
    assert all(len(frame) == FRAME_BYTES for frame in frames)
    assert 4 <= len(frames) <= 6  # 100ms, rounded to Discord frame geometry
    assert frames[0][:4] == struct.pack('<hh', 500, 500)
    source = PCMStream(AudioFormat(sample_rate=48000, channels=2))
    task = asyncio.create_task(source.write(b'\1' * FRAME_BYTES * 60))
    for _ in range(100):
        if source._frames.full():
            break
        await asyncio.sleep(.01)
    assert source._frames.full()
    source.cleanup()
    with pytest.raises(RuntimeError, match='cancelled'):
        await asyncio.wait_for(task, 2)
    assert source.read() == b''


@pytest.mark.asyncio
async def test_real_consumer_adapter_lifecycle_partial_error_no_replay(monkeypatch):
    calls = []
    class Provider:
        sample_rate = 24000
        channels = 1
        sample_width = 2
        def stream(self, text):
            calls.append(text)
            yield struct.pack('<h', 500) * 2400
            raise RuntimeError('controlled partial provider failure')
    monkeypatch.setattr('tools.tts_streaming.resolve_streaming_provider', lambda _: Provider())
    obj = make_adapter()
    consumer = StreamingTTSConsumer(obj, '789', {}, asyncio.get_running_loop())
    consumer.start()
    consumer.on_delta('<think>Never speak this.</think> This is a complete user-facing sentence. ')
    consumer.finish()
    assert not await consumer.wait_complete(3)
    assert consumer.partial and consumer.suppress_whole_file
    assert all('Never speak' not in call for call in calls)
    assert not obj._voice_streams
    obj._voice_clients[42].stop.assert_called()


@pytest.mark.asyncio
async def test_stream_stop_before_provider_first_chunk_never_restarts(monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    class Provider:
        def stream(self, text):
            entered.set()
            release.wait(3)
            yield b'\1' * 4800
    monkeypatch.setattr('tools.tts_streaming.resolve_streaming_provider', lambda _: Provider())
    obj = make_adapter()
    consumer = StreamingTTSConsumer(obj, '789', {}, asyncio.get_running_loop())
    consumer.start()
    consumer.on_delta('This is a complete sentence to synthesise. ')
    consumer.finish()
    assert await asyncio.to_thread(entered.wait, 2)
    await obj.stop_voice_playback(42)
    release.set()
    assert not await consumer.wait_complete(3)
    assert consumer.suppress_whole_file
    obj._voice_clients[42].play.assert_not_called()


@pytest.mark.asyncio
async def test_stream_completes_through_discord_player_reads(monkeypatch):
    class Provider:
        def stream(self, text):
            yield b'\1' * 4800
    monkeypatch.setattr('tools.tts_streaming.resolve_streaming_provider', lambda _: Provider())
    obj = make_adapter()
    pump_tasks = []
    async def pump(source):
        while source.read():
            await asyncio.sleep(.001)
    obj._voice_clients[42].play.side_effect = lambda source: pump_tasks.append(asyncio.create_task(pump(source)))
    consumer = StreamingTTSConsumer(obj, '789', {}, asyncio.get_running_loop())
    consumer.start()
    consumer.on_delta('This is the full user-facing answer. ')
    consumer.finish()
    assert await consumer.wait_complete(3)
    await asyncio.gather(*pump_tasks)
    assert consumer.suppress_whole_file
    assert not obj._voice_streams


def test_unclosed_reasoning_tail_and_isolated_latches():
    from tools.tts_streaming import SentenceChunker, SpeechInterruptionLatch
    chunker = SentenceChunker()
    chunker.feed('<think>private unfinished reasoning')
    assert chunker.flush() == []
    first, second = SpeechInterruptionLatch(), SpeechInterruptionLatch()
    first.mark()
    assert not second.take()
    assert first.take() and not first.take()


@pytest.mark.asyncio
async def test_first_chunk_write_partial_failure_suppresses_replay(monkeypatch):
    class Provider:
        def stream(self, text):
            yield b'\1' * 4800
    monkeypatch.setattr('tools.tts_streaming.resolve_streaming_provider', lambda _: Provider())
    async def partially_write_then_fail(self, chunk):
        await self._put(b'\1' * FRAME_BYTES)
        assert self.read() == b'\1' * FRAME_BYTES
        raise TimeoutError('controlled queue stall after playback')
    monkeypatch.setattr(PCMStream, 'write', partially_write_then_fail)
    obj = make_adapter()
    consumer = StreamingTTSConsumer(obj, '789', {}, asyncio.get_running_loop())
    consumer.start()
    consumer.on_delta('This sentence partly plays before a write failure. ')
    consumer.finish()
    assert not await consumer.wait_complete(3)
    assert consumer.partial and consumer.suppress_whole_file
