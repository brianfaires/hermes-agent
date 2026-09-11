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


async def wait_until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(.005)
    await asyncio.wait_for(poll(), 3)


class FixturePlayer:
    """Controlled Discord transport; real PCM sources/mixer execute every read."""
    def __init__(self, vc):
        self.source = None
        self.paused = False
        self.reads = []
        self.tasks = []
        vc.play.side_effect = self.play
        vc.is_playing.side_effect = lambda: self.source is not None and not self.paused
        vc.is_paused.side_effect = lambda: self.paused
        vc.pause.side_effect = lambda: setattr(self, 'paused', True)
        vc.resume.side_effect = lambda: setattr(self, 'paused', False)
        vc.stop.side_effect = lambda: setattr(self, 'source', None)

    def play(self, source, **kwargs):
        assert self.source is None, 'streamed clauses must never overlap'
        self.source = source
        self.paused = False
        self.tasks.append(asyncio.create_task(self.pump(source)))

    async def pump(self, source):
        try:
            while self.source is source:
                if not self.paused:
                    frame = source.read()
                    if not frame:
                        break
                    self.reads.append(frame)
                await asyncio.sleep(.001)
        finally:
            if self.source is source:
                self.source = None
            source.cleanup()

    async def close(self):
        self.source = None
        await asyncio.gather(*self.tasks)


def progress_speaker(obj, consumer, *, silence_seconds=.06):
    from unittest.mock import AsyncMock
    from gateway.run import DiscordVoiceProgressSpeaker
    runner = SimpleNamespace(_adapter_for_source=lambda _: obj,
                             _send_voice_reply=AsyncMock())
    event = SimpleNamespace(source=SimpleNamespace())
    speaker = DiscordVoiceProgressSpeaker(runner, event, loop=asyncio.get_running_loop(),
                                           guild_id=42, silence_seconds=silence_seconds,
                                           streaming_consumer=consumer)
    return speaker, runner


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['direct', 'mixer', 'ambient'])
async def test_open_stream_long_tool_uses_sparse_serial_progress(monkeypatch, tmp_path, mode):
    from plugins.platforms.discord.voice_mixer import VoiceMixer
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    calls = []
    class Provider:
        def stream(self, text):
            calls.append(text)
            yield struct.pack('<h', 500) * 2400
    monkeypatch.setattr('tools.tts_streaming.resolve_streaming_provider', lambda _: Provider())
    obj = make_adapter()
    vc = obj._voice_clients[42]
    player = FixturePlayer(vc)
    if mode != 'direct':
        mixer = VoiceMixer()
        obj._voice_mixers[42] = mixer
        if mode == 'ambient':
            mixer.set_ambient(b'\1' * FRAME_BYTES)
        vc.play(mixer)
    consumer = StreamingTTSConsumer(obj, '789', {}, asyncio.get_running_loop())
    speaker, runner = progress_speaker(obj, consumer, silence_seconds=18)
    consumer.start()
    try:
        consumer.on_delta('I will inspect the failing check before making changes. ')
        await wait_until(lambda: player.reads and consumer.audible)
        speaker.tool_started('tool-1', 'execute_code', {'secret': 'never narrate args'})
        await wait_until(lambda: not consumer._busy)
        assert not consumer.done  # text stream deliberately open during tool work
        handle = consumer._handle
        assert handle.clause_drained
        assert obj._voice_stream_playing(42)  # reserves against competing file playback
        assert calls == ['I will inspect the failing check before making changes.']
        if mode == 'ambient':
            assert vc.is_playing()  # explicitly configured ambient is retained
        else:
            assert not vc.is_playing(), 'no steady idle PCM transmission'
        # Advance only the silence anchors: no 18-second wall-clock sleep.
        consumer._last_activity_at -= 19
        speaker._silence_anchor -= 19
        await speaker._reset_timer(speaker._generation)
        await wait_until(lambda: len(calls) == 2)
        await wait_until(lambda: not consumer._busy)
        assert calls[1] == 'Still working on execute code.'
        assert consumer._handle is handle
        runner._send_voice_reply.assert_not_awaited()
        # A second timer invocation before the interval cannot enqueue speech.
        assert not await speaker._speak_once('Still working on execute code.')
        await speaker.close()
        consumer.on_delta('The check is complete and the correction is ready. ')
        consumer.finish()
        assert await consumer.wait_complete(3)
        assert consumer.suppress_whole_file
        assert calls == ['I will inspect the failing check before making changes.',
                         'Still working on execute code.',
                         'The check is complete and the correction is ready.']
        if mode != 'ambient':
            assert not vc.is_playing()
    finally:
        await speaker.close()
        consumer.abort()
        await consumer.wait_complete(3)
        await player.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalidate', ['complete', 'close', 'stop', 'disconnect'])
async def test_delayed_progress_cannot_outlive_tool_or_turn(monkeypatch, tmp_path, invalidate):
    import threading
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    class Provider:
        def stream(self, text):
            if text.startswith('Still working'):
                entered.set()
                release.wait(3)
                yield struct.pack('<h', 999) * 2400
            else:
                yield struct.pack('<h', 500) * 2400
    monkeypatch.setattr('tools.tts_streaming.resolve_streaming_provider', lambda _: Provider())
    obj = make_adapter()
    player = FixturePlayer(obj._voice_clients[42])
    consumer = StreamingTTSConsumer(obj, '789', {}, asyncio.get_running_loop())
    speaker, runner = progress_speaker(obj, consumer, silence_seconds=18)
    consumer.start()
    try:
        consumer.on_delta('I will inspect the failing check before making changes. ')
        await wait_until(lambda: consumer.audible and not consumer._busy)
        speaker.tool_started('tool-1', 'execute_code', {})
        consumer._last_activity_at -= 19
        speaker._silence_anchor -= 19
        await speaker._reset_timer(speaker._generation)
        assert await asyncio.to_thread(entered.wait, 2)
        if invalidate == 'complete':
            speaker.tool_completed('tool-1', 'execute_code')
        elif invalidate == 'close':
            await speaker.close()
        elif invalidate == 'stop':
            await obj.stop_voice_playback(42)
        else:
            obj._voice_clients[42].is_connected.return_value = False
        release.set()
        await wait_until(lambda: not consumer._busy)
        assert all(struct.pack('<hh', 999, 999) not in frame for frame in player.reads)
        await speaker.close()
        consumer.on_delta('The check is complete and the correction is ready. ')
        consumer.finish()
        complete = await consumer.wait_complete(3)
        assert complete == (invalidate in {'complete', 'close'})
        assert consumer.suppress_whole_file
        runner._send_voice_reply.assert_not_awaited()
    finally:
        release.set()
        await speaker.close()
        consumer.abort()
        await consumer.wait_complete(3)
        await player.close()


@pytest.mark.asyncio
async def test_unavailable_stream_keeps_legacy_progress(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('tools.tts_streaming.resolve_streaming_provider', lambda _: SimpleNamespace(sample_rate=123))
    obj = make_adapter()
    consumer = StreamingTTSConsumer(obj, '789', {}, asyncio.get_running_loop())
    speaker, runner = progress_speaker(obj, consumer)
    consumer.start()
    await consumer.wait_complete(3)
    assert consumer.done and not consumer.suppress_whole_file
    assert await speaker._speak_once('Still working on execute code.')
    runner._send_voice_reply.assert_awaited_once()
    await speaker.close()


@pytest.mark.asyncio
async def test_gateway_callbacks_wire_streamed_progress_and_final_suppression(monkeypatch, tmp_path):
    import threading
    from unittest.mock import AsyncMock
    from tests.gateway.test_run_progress_topics import _run_with_agent
    from gateway.run import DiscordVoiceProgressSpeaker, GatewayRunner
    from gateway.config import Platform
    from gateway.platforms.base import MessageType
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(DiscordVoiceProgressSpeaker, 'DEFAULT_SILENCE_SECONDS', .06)
    monkeypatch.setattr(GatewayRunner, '_gateway_loop', asyncio.get_running_loop(), raising=False)
    opening, progress = threading.Event(), threading.Event()
    calls = []
    class Provider:
        def stream(self, text):
            calls.append(text)
            if text.startswith('Still working'):
                progress.set()
            else:
                opening.set()
            yield struct.pack('<h', 500) * 2400
    monkeypatch.setattr('tools.tts_streaming.resolve_streaming_provider', lambda _: Provider())
    monkeypatch.setattr('tools.tts_tool._load_tts_config', lambda: {})
    obj = make_adapter()
    obj.send = AsyncMock()
    obj.send_typing = AsyncMock()
    obj.stop_typing = AsyncMock()
    player = FixturePlayer(obj._voice_clients[42])
    class Agent:
        def __init__(self, **kwargs):
            self.tools = []
        def run_conversation(self, *args, **kwargs):
            self.interim_assistant_callback('I will inspect the failing check first.')
            assert opening.wait(3)
            self.tool_start_callback('tool-1', 'execute_code', {'secret': 'hidden'})
            assert progress.wait(3), 'gateway must attach current consumer to progress speaker'
            self.tool_complete_callback('tool-1', 'execute_code', {}, 'complete')
            self.stream_delta_callback('The check is complete and ready for review. ')
            return {'final_response': 'The check is complete and ready for review.',
                    'messages': [], 'api_calls': 1}
    try:
        _, result, runner = await _run_with_agent(
            monkeypatch, tmp_path, Agent, session_id='stream-idle-progress',
            platform=Platform.DISCORD, chat_id='789', thread_id=None, guild_id='42',
            message_type=MessageType.VOICE, initial_voice_mode='voice_only',
            config_data={'display': {'interim_assistant_messages': True}},
            adapter_cls=lambda **_: obj, return_runner=True, run_generation=1,
        )
        assert result['final_response'] == 'The check is complete and ready for review.'
        assert obj._streaming_tts_turn_completed('agent:main:discord:group:789', 1)
        assert calls == ['I will inspect the failing check first.',
                         'Still working on execute code.',
                         'The check is complete and ready for review.']
        runner._send_voice_reply.assert_not_awaited()
    finally:
        progress.set()
        await player.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('next_speech', ['file', 'ack', 'abort'])
async def test_no_ambient_mixer_resumes_legacy_speech_and_pauses_abort(monkeypatch, tmp_path, next_speech):
    import json
    from plugins.platforms.discord.voice_mixer import VoiceMixer
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('tools.tts_streaming.resolve_streaming_provider',
                        lambda _: SimpleNamespace(stream=lambda text: iter([b'\1' * 4800])))
    obj = make_adapter()
    obj._voice_fx_cfg.update(ack_enabled=True, lead_silence_ms=0)
    vc = obj._voice_clients[42]
    player = FixturePlayer(vc)
    mixer = VoiceMixer()
    obj._voice_mixers[42] = mixer
    vc.play(mixer)
    consumer = StreamingTTSConsumer(obj, '789', {}, asyncio.get_running_loop())
    consumer.start()
    try:
        consumer.on_delta('This opening clause leaves the mixer idle. ')
        await wait_until(lambda: consumer.audible and not consumer._busy)
        assert vc.is_paused()
        if next_speech == 'abort':
            # Exercise interruption while the next clause is actively playing.
            handle = consumer._handle
            await obj.write_streaming_tts(handle, b'\1' * 4800)
            assert not vc.is_paused()
            await obj.stop_voice_playback(42)
            assert vc.is_paused()
            assert not mixer.speech_active
        else:
            consumer.finish()
            assert await consumer.wait_complete(3)
            audio = tmp_path / 'fixture.mp3'
            audio.write_bytes(b'fixture')
            monkeypatch.setattr('plugins.platforms.discord.voice_mixer.decode_to_pcm',
                                lambda _: b'\2' * FRAME_BYTES * 3)
            monkeypatch.setattr('tools.tts_tool.text_to_speech_tool',
                                lambda **_: json.dumps({'success': True, 'file_path': str(audio)}))
            before = len(player.reads)
            if next_speech == 'ack':
                assert await obj.play_ack_in_voice(42, 'Working.')
                await wait_until(lambda: not mixer.speech_active)
            else:
                assert await obj.play_in_voice_channel(42, str(audio))
            assert len(player.reads) > before
            vc.resume.assert_called()
    finally:
        consumer.abort()
        await consumer.wait_complete(3)
        await player.close()


@pytest.mark.asyncio
async def test_progress_rechecks_silence_after_worker_dequeues_model_clause(monkeypatch, tmp_path):
    import threading
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    removed, release = threading.Event(), threading.Event()
    calls = []
    class Provider:
        def stream(self, text):
            calls.append(text)
            yield b'\1' * 4800
    monkeypatch.setattr('tools.tts_streaming.resolve_streaming_provider', lambda _: Provider())
    obj = make_adapter()
    player = FixturePlayer(obj._voice_clients[42])
    consumer = StreamingTTSConsumer(obj, '789', {}, asyncio.get_running_loop())
    get = consumer._queue.get
    def delayed_get(*args, **kwargs):
        item = get(*args, **kwargs)
        if isinstance(item, str):
            removed.set()
            release.wait(3)
        return item
    monkeypatch.setattr(consumer._queue, 'get', delayed_get)
    consumer.on_delta('Here is a useful model clause that takes priority. ')
    consumer.start()
    try:
        assert await asyncio.to_thread(removed.wait, 2)
        consumer._last_activity_at -= 19
        assert consumer.enqueue_progress('Still working on execute code.',
                                         is_current=lambda: True, silence_seconds=18)
        release.set()
        await wait_until(lambda: consumer.audible and not consumer._busy and not consumer._progress_pending)
        assert calls == ['Here is a useful model clause that takes priority.']
        consumer.finish()
        assert await consumer.wait_complete(3)
    finally:
        release.set()
        consumer.abort()
        await consumer.wait_complete(3)
        await player.close()
