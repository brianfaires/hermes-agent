"""Bundled, opt-in subscriber for cross-profile cron lifecycle events."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from .subscriber import EventSubscriber

logger = logging.getLogger(__name__)


def _settings() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
        if not isinstance(config, Mapping):
            return {}
        plugins = config.get("plugins") or {}
        if not isinstance(plugins, Mapping):
            return {}
        entries = plugins.get("entries") or {}
        if not isinstance(entries, Mapping):
            return {}
        settings = entries.get("cron-event-subscriber") or {}
        return dict(settings) if isinstance(settings, Mapping) else {}
    except Exception:
        return {}


def _bounded_int(
    settings: Mapping[str, Any],
    key: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int,
) -> int:
    value = settings.get(key, default)
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(maximum, max(minimum, parsed))


def _subscriber() -> EventSubscriber:
    from cron.event_bus import event_root

    settings = _settings()
    return EventSubscriber(
        event_root(),
        claim_timeout_seconds=_bounded_int(
            settings, "claim_timeout_seconds", 300, maximum=86400
        ),
        retention_days=_bounded_int(
            settings, "retention_days", 30, maximum=3650
        ),
        temporary_retention_seconds=_bounded_int(
            settings, "temporary_retention_seconds", 3600, maximum=604800
        ),
    )


def _on_session_start(**_kwargs: Any) -> None:
    """Use an existing lifecycle hook for recovery and cleanup maintenance."""

    try:
        result = _subscriber().maintain()
        if result.recovered or result.cleaned:
            logger.info(
                "cron-event-subscriber maintenance: recovered=%d cleaned=%d",
                result.recovered,
                result.cleaned,
            )
    except Exception:
        logger.exception("cron-event-subscriber maintenance failed")


def _handle_command(raw_args: str) -> str:
    if raw_args.strip():
        return "Usage: /cron-events"
    settings = _settings()
    limit = _bounded_int(
        settings, "max_events_per_drain", 100, minimum=1, maximum=10000
    )
    events: list[dict[str, Any]] = []
    result = _subscriber().drain(
        lambda event: events.append(dict(event)),
        limit=limit,
    )
    payload = {
        "events": events,
        "processed": result.processed,
        "failed": result.failed,
        "duplicates": result.duplicates,
        "malformed": result.malformed,
        "recovered": result.recovered,
        "cleaned": result.cleaned,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def register(ctx) -> None:
    """Wire the subscriber through existing plugin command/lifecycle APIs."""

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_command(
        "cron-events",
        handler=_handle_command,
        description="Consume and acknowledge pending cross-profile cron lifecycle events",
    )
