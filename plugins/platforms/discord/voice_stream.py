"""Bounded provider PCM conversion and Discord 20ms audio source."""
import asyncio
import queue
import threading
import time

import discord

FRAME_BYTES = 3840


class PCMStream(discord.AudioSource):
    """At most one second of queued 48kHz stereo PCM; silence on underrun."""

    def __init__(self, audio_format, *, gain=1.0):
        import audioop
        self._audioop = audioop
        self.format = audio_format
        self.gain = gain
        self._frames = queue.Queue(maxsize=50)
        self._input_tail = b""
        self._output_tail = b""
        self._rate_state = None
        self._closed = threading.Event()
        self._eof = threading.Event()
        self.drained = threading.Event()
        self.finished = False
        self.audible = False

    async def _put(self, frame):
        deadline = time.monotonic() + 10
        while not self._closed.is_set():
            try:
                self._frames.put_nowait(frame)
                return
            except queue.Full:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Discord PCM queue did not drain")
                await asyncio.sleep(0.01)
        raise RuntimeError("Discord PCM stream cancelled")

    async def write(self, chunk):
        if self._closed.is_set() or self._eof.is_set():
            raise RuntimeError("Discord PCM stream closed")
        if len(chunk) > 1024 * 1024:
            raise ValueError("Oversized provider PCM chunk")
        # Provider chunks may split a sample; retain at most one input frame.
        data = self._input_tail + chunk
        alignment = self.format.channels * self.format.sample_width
        usable = len(data) - len(data) % alignment
        self._input_tail, data = data[usable:], data[:usable]
        if not data:
            return
        data, self._rate_state = self._audioop.ratecv(
            data, 2, self.format.channels, self.format.sample_rate, 48000,
            self._rate_state,
        )
        if self.format.channels == 1:
            data = self._audioop.tostereo(data, 2, 1, 1)
        data = self._output_tail + data
        usable = len(data) - len(data) % FRAME_BYTES
        for offset in range(0, usable, FRAME_BYTES):
            await self._put(data[offset:offset + FRAME_BYTES])
        self._output_tail = data[usable:]

    async def finish(self):
        if self._input_tail:
            raise ValueError("Truncated provider PCM sample")
        if self._output_tail:
            await self._put(self._output_tail.ljust(FRAME_BYTES, b"\0"))
            self._output_tail = b""
        self._eof.set()

    def read(self):
        if self._closed.is_set():
            self.finished = True
            self.drained.set()
            return b""
        try:
            frame = self._frames.get_nowait()
        except queue.Empty:
            if self._eof.is_set():
                self.finished = True
                self.drained.set()
                return b""
            return b"\0" * FRAME_BYTES
        self.audible = True
        return frame

    def read_frame(self):
        # Existing VoiceMixer child contract, preserving its ambient bed.
        import numpy as np
        frame = self.read()
        return np.frombuffer(frame, dtype=np.int16).astype(np.float32) * self.gain if frame else None

    def cleanup(self):
        self._closed.set()
        while True:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                break
        self.drained.set()
