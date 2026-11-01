"""Route metadata and activation facts shared by auxiliary fallback entry points."""
from __future__ import annotations

import re
from typing import Any, Optional


def _record_route_info(
    route_info: Optional[dict[str, str]], provider: Optional[str], model: Optional[str]
) -> None:
    """Expose the concrete route selected for one auxiliary call."""
    if route_info is not None:
        route_info['provider'] = provider or 'auto'
        route_info['model'] = model or 'default'


def _fallback_provider_from_label(label: str) -> str:
    """Recover the provider identifier from a fallback display label."""
    match = re.match(r'(?:fallback_chain\[\d+\]|fallback_providers\[\d+\]|main-agent)\(([^)]+)\)$', label or '')
    return match.group(1).strip() if match else str(label or '').strip()


def _effective_provider_for_client(client: Any, fallback: str) -> str:
    """Return the concrete provider selected for an auto-routed client."""
    effective_provider = getattr(client, '_hermes_aux_effective_provider', '')
    if isinstance(effective_provider, str) and effective_provider:
        return effective_provider
    return str(fallback or '')


def emit_auxiliary_fallback(old_provider, old_model, provider, model, *, task, reason):
    """Emit a selected auxiliary route; reason is a fixed label, never an exception."""
    from agent.fallback_events import emit_fallback_activated
    emit_fallback_activated(
        old_provider=_fallback_provider_from_label(old_provider), old_model=old_model,
        new_provider=_fallback_provider_from_label(provider), new_model=model,
        stage='auxiliary', task=task, reason=reason,
    )
