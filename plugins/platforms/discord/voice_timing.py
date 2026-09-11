"""Bounded local voice timing observations; no identifiers, text or audio."""
from collections import deque
import threading
import time


class VoiceTiming:
    """An in-memory diagnostic ring. Values are monotonic seconds, not latency claims."""

    STAGES = frozenset({
        "end_of_speech", "endpoint_ready", "stt_ready", "first_model_text",
        "playback_started", "speech_onset", "stop_complete",
    })

    def __init__(self):
        self._samples = deque(maxlen=128)
        self._lock = threading.Lock()

    def mark(self, stage, timestamp=None):
        if stage not in self.STAGES:
            raise ValueError("Unknown voice timing stage")
        with self._lock:
            self._samples.append((stage, time.monotonic() if timestamp is None else float(timestamp)))

    def snapshot(self):
        with self._lock:
            return tuple(self._samples)
