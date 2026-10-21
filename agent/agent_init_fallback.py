"""Init-time fallback chain: entry normalization, chain setup, and the fallback's api_mode."""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any, Dict, List, Optional

logger = logging.getLogger("run_agent")


def _fallback_entries(fallback_model) -> list[dict[str, Any]]:
    """Normalize legacy single-dict ``fallback_model`` / list ``fallback_providers``."""
    if isinstance(fallback_model, dict):
        fallback_model = [fallback_model]
    if not isinstance(fallback_model, list):
        return []
    return [
        f for f in fallback_model if isinstance(f, dict) and f.get("provider") and f.get("model")
    ]


def _init_fallback_chain(agent, fallback_model):
    # Stable pool-entry identity: OAuth refreshes can replace the token before a failed
    # request is recovered, so the key value alone can't attribute the failure.
    from agent.agent_runtime_helpers import sync_credential_pool_entry_id
    sync_credential_pool_entry_id(agent)

    # Ordered backups tried when the primary is exhausted (legacy single-dict or list).
    agent._fallback_chain = _fallback_entries(fallback_model)
    agent._fallback_index = 0
    agent._fallback_activated = getattr(agent, "_fallback_activated", False)
    # Legacy attribute kept for backward compat (tests, external callers)
    agent._fallback_model = agent._fallback_chain[0] if agent._fallback_chain else None
    chain = agent._fallback_chain
    if chain and not agent.quiet_mode:
        labels = [f"{f['model']} ({f['provider']})" for f in chain]
        if len(chain) == 1:
            print(f"🔄 Fallback model: {labels[0]}")
        else:
            print(f"🔄 Fallback chain ({len(chain)} providers): " + " → ".join(labels))


def recompute_init_fallback_api_mode(agent, fb_client) -> None:
    """Give an init-time fallback its own api_mode instead of the unreachable primary's.

    A Nous primary configured with ``api_mode: codex_responses`` otherwise leaves a Copilot
    gpt-5-mini fallback on the Responses API, which silently drops reasoning content (#46527).
    The per-turn path (``try_activate_fallback``) already recomputes via
    ``_fallback_api_mode_resolved``; this keeps init in agreement, and runs before the
    ``_primary_runtime`` snapshot so ``restore_primary_runtime`` replays the recomputed mode.
    A detection failure keeps the primary's mode and never blocks the fallback itself.
    """
    from agent.chat_completion_helpers import _fallback_api_mode_resolved
    try:
        agent.api_mode = _fallback_api_mode_resolved(
            agent, agent.provider, agent.model, str(getattr(fb_client, "base_url", "") or ""))
    except Exception:
        logger.debug("Init-time fallback api_mode detection failed for %s", agent.provider, exc_info=True)
        return
    if hasattr(agent, "_transport_cache"):
        agent._transport_cache.clear()


def routed_client_kwargs(agent, fallback_model, _provider_timeout) -> Optional[dict[str, Any]]:
    """OpenAI-client kwargs via the centralized provider router (no explicit creds).

    Falls through to the init-time fallback chain, then raises with the missing-key /
    no-provider diagnostic. ``None`` when the chain landed on a MoA preset: the facade is
    already bound and there is no OpenAI client to construct.
    """
    from agent.auxiliary_client import resolve_provider_client
    from agent.fallback_events import emit_fallback_activated
    old_model, old_provider = agent.model, agent.provider
    _routed_client, _ = resolve_provider_client(
        agent.provider or "auto", model=agent.model, raw_codex=True)
    if _routed_client is not None:
        from hermes_cli.providers import is_actual_route, normalize_provider
        effective_provider = getattr(_routed_client, "_hermes_aux_effective_provider", "")
        if is_actual_route(effective_provider):
            agent.provider = normalize_provider(effective_provider)
        from agent.agent_init import _client_kwargs_from_routed
        return _client_kwargs_from_routed(_routed_client, _provider_timeout)
    # No credentials: try the fallback chain BEFORE failing (an exhausted single-entry pool
    # must not die with a misleading "No LLM provider configured"); only explicitly named
    # providers keep the missing-key diagnostic.
    # An exhausted single-entry pool (typically ``openrouter`` under free-tier daily quotas) must still
    # reach the chain instead of dying at init with a misleading "No LLM provider configured" error. See
    # #17929.
    _explicit = (agent.provider or "").strip().lower()
    _refused_entries = []
    for _fb in _fallback_entries(fallback_model):
        _fb_provider = str(_fb["provider"])
        try:
            from hermes_cli.fallback_config import resolve_entry_api_key
            _fb_explicit_key = resolve_entry_api_key(_fb)
            _fb_client, _fb_model = resolve_provider_client(
                _fb["provider"], model=_fb["model"], raw_codex=True,
                explicit_base_url=_fb.get("base_url"), explicit_api_key=_fb_explicit_key,
            )
        except Exception as _fb_exc:
            logger.debug("Init-time fallback entry %s failed: %s", _fb_provider, _fb_exc, exc_info=True)
            # A bare exception (``KeyError()``) stringifies empty; name its type instead.
            _refused_entries.append((_fb_provider, str(_fb_exc) or type(_fb_exc).__name__))
            continue
        if _fb_client is None:
            # The router returns None when no credentials are usable for the entry — a skip
            # that leaves no trace otherwise, hiding key-less fallback entries from the log.
            logger.debug(
                "Init-time fallback entry %s resolved no usable credentials", _fb_provider
            )
            _refused_entries.append((_fb_provider, "no usable credentials"))
            continue
        agent._fallback_activated = True
        if _fb_provider.strip().lower() == "moa":
            # The chokepoint handed back the preset's aggregator client, which only proves the
            # preset resolves and its aggregator has credentials. A MoA entry means the preset
            # itself (same as ``provider: moa`` in config), so bind the facade, not the aggregator.
            from agent.moa_loop import bind_moa_runtime
            bind_moa_runtime(agent, _fb["model"])
            emit_fallback_activated(old_provider=old_provider, old_model=old_model,
                                    new_provider=agent.provider, new_model=agent.model, stage="init")
            return None
        agent.provider = agent.requested_provider = _fb["provider"]
        agent.model = _fb_model or _fb["model"]
        recompute_init_fallback_api_mode(agent, _fb_client)
        from agent.agent_init import _client_kwargs_from_routed
        kwargs = _client_kwargs_from_routed(_fb_client, _provider_timeout)
        emit_fallback_activated(old_provider=old_provider, old_model=old_model,
                                new_provider=agent.provider, new_model=agent.model, stage="init")
        return kwargs
    _raise_resolution_failure(agent, _explicit, _refused_entries)


def _raise_resolution_failure(agent, _explicit, _refused_entries):
    # A burned credential pool (#119533) is otherwise indistinguishable from missing config,
    # so name it even when no fallback entries are configured.
    _pool_exhausted = False
    _pool = None
    if _explicit and _explicit != "auto":
        with suppress(Exception):
            from agent.credential_pool import load_pool
            _pool = load_pool(_explicit)
            _pool_exhausted = _pool.has_credentials() and not _pool.has_available(model=agent.model)
    if _refused_entries or _pool_exhausted:
        # Neutral wording: the explicit-provider branch below raises the provider-specific
        # missing-credentials message, not the generic "No LLM provider configured" one.
        logger.warning(
            "Init-time provider resolution failed: primary %r unresolvable (%s); fallback entries refused: %s",
            agent.provider,
            "credential pool exhausted" if _pool_exhausted else "no usable credentials",
            "; ".join(f"{_p} ({_r})" for _p, _r in _refused_entries) or "none configured",
        )
    # A fully-exhausted pool is a billing/quota failure, NOT a config problem: name it for EVERY
    # explicit provider. ``openrouter`` / ``custom`` have no provider-specific missing-credentials
    # branch, so a burned override pool there fell through to the generic "No LLM provider
    # configured" setup message even though the config default was fine (#94785). Prefer a billing
    # verdict (402 / classifier "billing") and fall back to the existing cooldown wording (which
    # names the 429 reset time, #56810); raise before the missing-credentials branch so a genuine
    # 402 is never described as a missing key or a transient rate limit.
    if _pool_exhausted:
        from agent.auxiliary_unavailable import (
            ProviderCredentialsExhaustedError,
            pool_billing_message,
            pool_cooldown_message,
        )
        _exhausted_message = (
            pool_billing_message(_explicit, model=agent.model, pool=_pool)
            or pool_cooldown_message(_explicit)
        )
        if _exhausted_message:
            raise ProviderCredentialsExhaustedError(_exhausted_message, provider=_explicit)
    if _explicit and _explicit not in {"auto", "openrouter", "custom"}:
        # Explicit non-OpenRouter provider with no creds and no usable fallback: fail fast.
        from agent.auxiliary_unavailable import ProviderNotConfiguredError, missing_provider_credentials_message
        raise ProviderNotConfiguredError(missing_provider_credentials_message(_explicit))
    from hermes_constants import profile_cli_selector
    from agent.auxiliary_unavailable import ProviderNotConfiguredError
    _sel = profile_cli_selector()
    raise ProviderNotConfiguredError(
        "No LLM provider configured. Run `hermes model` to "
        "select a provider, or run `hermes setup` for first-time "
        "configuration."
    )
