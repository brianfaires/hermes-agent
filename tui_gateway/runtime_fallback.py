"""TUI/Desktop runtime resolution with explicit fallback activation facts."""
from __future__ import annotations

import logging
from typing import NamedTuple


class RuntimeFallbackResolution(NamedTuple):
    runtime: dict
    selected_model: str | None
    used_fallback: bool


def resolve_runtime_with_fallback(resolve_kwargs: dict | None = None) -> RuntimeFallbackResolution:
    """Resolve the primary runtime or one complete provider/model fallback. Provider-only fallback entries
    are skipped so the unavailable primary model can never leak into a different runtime."""
    from hermes_cli.auth import AuthError
    from tui_gateway.server import _load_fallback_model
    from agent.fallback_events import emit_fallback_activated
    from hermes_cli.runtime_provider import resolve_runtime_provider
    try:
        return RuntimeFallbackResolution(resolve_runtime_provider(**(resolve_kwargs or {})), None, False)
    except AuthError as primary_exc:
        for entry in _load_fallback_model() or []:
            fb_provider = str(entry.get("provider") or "").strip() if isinstance(entry, dict) else ""
            fb_model = str(entry.get("model") or "").strip() if isinstance(entry, dict) else ""
            if not fb_provider or not fb_model:
                continue
            try:
                from hermes_cli.fallback_config import effective_runtime_provider, resolve_entry_api_key
                fb_kwargs: dict = {"requested": fb_provider, "target_model": fb_model,
                                   **({"explicit_base_url": entry["base_url"]} if entry.get("base_url") else {})}
                if fb_api_key := resolve_entry_api_key(entry):
                    fb_kwargs["explicit_api_key"] = fb_api_key
                runtime = resolve_runtime_provider(**fb_kwargs)
                # Named custom entries resolve to the bare "custom" billing class; keep the configured
                # identity so the session/UI shows the provider name, matching the manual-switch path (#98739).
                runtime["provider"] = effective_runtime_provider(entry, runtime)
                from hermes_cli.auth import primary_failure_wording
                logging.getLogger(__name__).warning(
                    "Primary %s (%s), falling back to %s model %s",
                    primary_failure_wording(primary_exc)[0], primary_exc, fb_provider, fb_model)
                emit_fallback_activated(
                    old_provider=(resolve_kwargs or {}).get("requested", ""),
                    old_model=(resolve_kwargs or {}).get("target_model", ""),
                    new_provider=runtime["provider"], new_model=fb_model,
                    stage="tui_runtime", reason="auth_resolution", platform="tui")
                return RuntimeFallbackResolution(runtime, fb_model, True)
            except Exception:
                logging.getLogger(__name__).debug("TUI fallback candidate refused", exc_info=True)
                continue
        raise

