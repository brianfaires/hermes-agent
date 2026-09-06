"""Fallback activation event emission.

Kept separate from provider-switching code so every real fallback path can
share one shell/plugin hook primitive without growing the model tool schema.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _reason_label(reason: Any) -> str:
    """Return a non-sensitive reason label.

    Callers pass structured enums or small stage labels here, not exception
    objects. If a future caller accidentally passes a verbose object, prefer
    its enum-ish name/value over stringifying potentially sensitive text.
    """
    value = getattr(reason, "value", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    name = getattr(reason, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    if isinstance(reason, str):
        return reason.strip()
    return ""


def active_profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return _clean(get_active_profile_name()) or "default"
    except Exception:
        return "default"


def emit_fallback_activated(
    *,
    old_provider: Any = "",
    old_model: Any = "",
    new_provider: Any = "",
    new_model: Any = "",
    stage: str,
    reason: Any = None,
    session_id: Any = "",
    platform: Any = "",
    api_mode: Any = "",
) -> None:
    """Fire the fallback_activated hook, swallowing hook failures.

    The event is operational telemetry for local hooks. It deliberately avoids
    raw exceptions, API keys, base URLs, request payloads, and response bodies.
    """
    try:
        from hermes_cli.plugins import invoke_hook

        invoke_hook(
            "fallback_activated",
            session_id=_clean(session_id),
            old_provider=_clean(old_provider),
            old_model=_clean(old_model),
            provider=_clean(new_provider),
            model=_clean(new_model),
            fallback_provider=_clean(new_provider),
            fallback_model=_clean(new_model),
            stage=_clean(stage),
            reason=_reason_label(reason),
            platform=_clean(platform),
            api_mode=_clean(api_mode),
            profile_name=active_profile_name(),
        )
    except Exception:
        logger.debug("fallback_activated hook emission failed", exc_info=True)
