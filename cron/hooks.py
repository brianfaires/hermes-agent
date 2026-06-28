"""Generic lifecycle hooks for cron jobs.

Other subsystems (e.g. ``cron_calendar_sync``) register callbacks that fire
when a cron job is created, updated, removed, or finishes a run. The four
events are:

    CREATE    — a new job was created            payload: job
    UPDATE    — an existing job was updated       payload: job
    REMOVE    — a job was removed                 payload: job
    COMPLETE  — a job finished a run              payload: job, success,
                                                  duration_seconds, error,
                                                  notify

Hooks are *best effort*: each callback runs inside its own try/except so a
faulty hook can never break a job mutation or a scheduler run. Callbacks
receive keyword arguments and should accept ``**kwargs`` (or the specific keys
they use) so new payload fields can be added without breaking existing hooks.

When ``cron.events.enabled`` is true for the active profile (or
``HERMES_CRON_EVENTS_ENABLED=1`` is set), the same lifecycle events are also
published as redacted JSONL records under the shared Hermes root so other
profiles can observe them without directly mutating this profile's cron state.
"""

import logging
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

CREATE = "create"
UPDATE = "update"
REMOVE = "remove"
COMPLETE = "complete"

EVENTS = (CREATE, UPDATE, REMOVE, COMPLETE)

_hooks: Dict[str, List[Callable]] = {event: [] for event in EVENTS}


def register_hook(event: str, callback: Callable) -> None:
    """Register ``callback`` to fire on ``event``. Idempotent per callback."""
    if event not in _hooks:
        raise ValueError(
            f"Unknown cron hook event {event!r}; expected one of {EVENTS}"
        )
    if callback not in _hooks[event]:
        _hooks[event].append(callback)


def unregister_hook(event: str, callback: Callable) -> None:
    """Remove a previously-registered callback. No-op if not registered."""
    if event in _hooks and callback in _hooks[event]:
        _hooks[event].remove(callback)


def clear_hooks(event: str = None) -> None:
    """Remove all callbacks for ``event`` (or every event when ``event`` is None).

    Primarily for tests that need a clean registry between cases.
    """
    if event is None:
        for registered in _hooks.values():
            registered.clear()
    elif event in _hooks:
        _hooks[event].clear()


def _publish_cross_profile_event(event: str, payload: Dict[str, Any]) -> None:
    """Best-effort JSONL publication for cross-profile cron observers."""

    job = payload.get("job")
    if not isinstance(job, dict):
        return
    try:
        from cron import event_bus

        if not event_bus.events_enabled():
            return
        extra: Dict[str, Any] = {}
        for key in ("success", "duration_seconds"):
            if key in payload:
                extra[key] = payload[key]
        if "error" in payload:
            # Do not copy free-form error text into a cross-profile stream; it
            # may contain prompt snippets, paths, provider payloads, or chat IDs.
            extra["error_present"] = bool(payload.get("error"))
        event_bus.publish_cron_event(
            event,
            job=job,
            extra=extra or None,
        )
    except Exception:
        logger.exception("cron %s cross-profile event publication failed", event)


def emit(event: str, **payload) -> None:
    """Fire all callbacks registered for ``event`` with ``payload`` kwargs.

    A callback raising is logged and swallowed so one bad hook neither stops
    the other hooks nor propagates into the calling mutation/run.
    """
    if event not in _hooks:
        raise ValueError(
            f"Unknown cron hook event {event!r}; expected one of {EVENTS}"
        )
    _publish_cross_profile_event(event, payload)
    for callback in list(_hooks[event]):
        try:
            callback(**payload)
        except Exception:
            logger.exception(
                "cron %s hook %r failed",
                event,
                getattr(callback, "__name__", repr(callback)),
            )
