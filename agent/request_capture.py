"""Session-scoped, one-shot API request capture state.

This is intentionally process-local: captures are a debugging aid and must not
survive a gateway restart or become part of persisted conversation state.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_armed: set[str] = set()


def arm_request_capture(session_key: str) -> None:
    """Arm the next request for *session_key* (idempotently)."""
    if not session_key:
        raise ValueError("session_key is required")
    with _lock:
        _armed.add(session_key)


def consume_request_capture(session_key: str | None) -> bool:
    """Atomically consume an armed capture, returning whether it was armed."""
    if not session_key:
        return False
    with _lock:
        if session_key not in _armed:
            return False
        _armed.remove(session_key)
        return True


def clear_request_captures() -> None:
    """Clear process-local state (primarily useful for tests)."""
    with _lock:
        _armed.clear()
