"""Route metadata and activation facts shared by auxiliary fallback entry points."""
from __future__ import annotations

import re
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, NamedTuple, Optional


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
    """Dispatch a selected route off the inference thread, under its owning profile.

    Non-daemon work finishes on normal one-shot shutdown; hook subprocess timeouts
    still apply. A slow observer must not spend compression's inference deadline.
    """
    try:
        from agent.fallback_events import active_profile_name, emit_fallback_activated
        from agent.memory_provider import spawn_context_thread
        from hermes_cli.lifecycle import has_hook
        if not has_hook('fallback_activated'):
            return
        thread = spawn_context_thread(
            emit_fallback_activated, name='auxiliary-fallback-notification', daemon=False,
            kwargs=dict(old_provider=_fallback_provider_from_label(old_provider), old_model=old_model,
                        new_provider=_fallback_provider_from_label(provider), new_model=model,
                        stage='auxiliary', task=task, reason=reason, profile_name=active_profile_name()),
        )
        thread.start()
    except Exception as exc:
        logging.getLogger(__name__).warning('Auxiliary fallback observer dispatch failed (%s)', type(exc).__name__,
                                            exc_info=True)


_SUMMARY_FALLBACK = ContextVar('summary_fallback_activation', default=None)


@contextmanager
def summary_fallback_scope(origin):
    """Carry one summary retry's reason until a usable request route is resolved."""
    token = _SUMMARY_FALLBACK.set(origin)
    try:
        yield
    finally:
        _SUMMARY_FALLBACK.reset(token)


def summary_fallback_pending():
    return _SUMMARY_FALLBACK.get() is not None


def emit_resolved_summary_fallback(provider, model):
    origin = _SUMMARY_FALLBACK.get()
    if origin is not None:
        _SUMMARY_FALLBACK.set(None)
        old_provider, old_model, reason = origin
        emit_auxiliary_fallback(old_provider, old_model, provider, model, task='compression', reason=reason)


class _ResolvedAuxRoute(NamedTuple):
    client: Any
    final_model: Optional[str]
    resolved_provider: str
    effective_provider: str


def remember_auto_fallback(client, provider, model):
    """Keep fallback provenance alongside the existing cached-client route metadata."""
    try:
        client._hermes_aux_fallback_origin = (provider, model)
    except (AttributeError, TypeError):
        pass  # Availability probe stubs need not retain request metadata.
    return client
