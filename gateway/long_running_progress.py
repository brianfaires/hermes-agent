"""Long-running gateway turn heartbeat policy."""

from __future__ import annotations

from typing import Any, Mapping


def _coerce_nonnegative_float(value: Any, default: float | None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return None
    return parsed


def heartbeat_interval_seconds(
    config: Mapping[str, Any] | None,
    *,
    platform_key: str | None = None,
    env_value: str | None = None,
    default: float = 120.0,
) -> float | None:
    """Return the long-running heartbeat interval, or None when disabled."""

    if env_value not in (None, ""):
        return _coerce_nonnegative_float(env_value, default)

    cfg = config if isinstance(config, Mapping) else {}
    display = cfg.get("display") if isinstance(cfg.get("display"), Mapping) else {}
    platforms = display.get("platforms") if isinstance(display.get("platforms"), Mapping) else {}
    platform_cfg = platforms.get(platform_key) if platform_key and isinstance(platforms.get(platform_key), Mapping) else {}

    for section, key in (
        (platform_cfg, "gateway_notify_interval"),
        (display, "gateway_notify_interval"),
        (cfg.get("agent") if isinstance(cfg.get("agent"), Mapping) else {}, "gateway_notify_interval"),
    ):
        if key in section:
            return _coerce_nonnegative_float(section.get(key), default)

    return default


def heartbeat_text(*, elapsed_seconds: float, status_detail: str = "") -> str:
    """Build concise, escalating text for a still-running turn."""

    elapsed_minutes = max(1, int(elapsed_seconds // 60))
    detail = f" — {status_detail.strip()}" if status_detail.strip() else ""

    if elapsed_seconds >= 600:
        return f"I don’t have a clean result yet — {elapsed_minutes} min in. I’m still looking into it{detail}."
    if elapsed_seconds >= 360:
        return f"Still working — {elapsed_minutes} min in. This is taking longer than it should{detail}."
    return f"Still working — {elapsed_minutes} min in{detail}."


def should_send_voice_heartbeat(event: Any) -> bool:
    """Return True when a heartbeat should also be spoken into Discord voice."""

    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    platform_value = getattr(platform, "value", platform)
    message_type = getattr(event, "message_type", None)
    message_type_value = getattr(message_type, "value", message_type)

    return str(platform_value).lower() == "discord" and str(message_type_value).lower() == "voice"
