"""Deterministic gateway-side voice summaries for delivered text replies.

This module is intentionally not a model-facing tool.  The gateway calls it
*after* a normal text send succeeds, then sends the generated audio as a second
message when configured.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_PLATFORMS = {"telegram", "discord"}


def _platform_name(platform: Any) -> str:
    value = getattr(platform, "value", platform)
    return str(value or "").strip().lower()


def _voice_summary_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    raw = config.get("voice_summary")
    if not isinstance(raw, dict):
        # Tolerate the plural spelling from earlier notes/tests without making
        # it the documented config shape.
        raw = config.get("voice_summaries")
    return raw if isinstance(raw, dict) else {}


def voice_summary_enabled_for(
    *,
    platform: Any,
    chat_id: str | None,
    config: dict[str, Any] | None,
) -> bool:
    """Return whether voice summaries should run for this delivery."""
    cfg = _voice_summary_config(config)
    if not cfg.get("enabled", False):
        return False

    platform_key = _platform_name(platform)
    if not platform_key:
        return False

    raw_platforms = cfg.get("platforms", sorted(_DEFAULT_PLATFORMS))
    if raw_platforms in (None, ""):
        platforms = _DEFAULT_PLATFORMS
    elif isinstance(raw_platforms, str):
        platforms = {p.strip().lower() for p in raw_platforms.split(",") if p.strip()}
    else:
        try:
            platforms = {str(p).strip().lower() for p in raw_platforms if str(p).strip()}
        except TypeError:
            platforms = _DEFAULT_PLATFORMS
    if platforms and platform_key not in platforms:
        return False

    chat = str(chat_id or "")
    disabled_chats = {str(c) for c in (cfg.get("disabled_chats") or [])}
    if chat and chat in disabled_chats:
        return False
    enabled_chats = {str(c) for c in (cfg.get("enabled_chats") or [])}
    if enabled_chats and chat not in enabled_chats:
        return False

    return True


def _configured_model(config: dict[str, Any], cfg: dict[str, Any]) -> str:
    explicit = str(cfg.get("model") or "").strip()
    if explicit:
        return explicit
    model_cfg = config.get("model")
    if isinstance(model_cfg, dict):
        return str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
    if isinstance(model_cfg, str):
        return model_cfg.strip()
    return ""


def _default_summarize_text(text: str, *, cfg: dict[str, Any], config: dict[str, Any]) -> str:
    """Summarize outgoing text with an isolated no-tool agent call."""
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from run_agent import AIAgent

    requested_provider = str(cfg.get("provider") or os.getenv("HERMES_INFERENCE_PROVIDER") or "").strip() or None
    runtime = resolve_runtime_provider(requested=requested_provider)
    model = _configured_model(config, cfg) or str(runtime.get("model") or "").strip()
    agent = AIAgent(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        command=runtime.get("command"),
        args=list(runtime.get("args") or []),
        credential_pool=runtime.get("credential_pool"),
        model=model,
        max_iterations=1,
        enabled_toolsets=[],
        quiet_mode=True,
        skip_memory=True,
        skip_context_files=True,
    )
    agent._print_fn = lambda *args, **kwargs: None
    prompt = (
        "Rewrite this outgoing assistant message as a concise spoken summary.\n"
        "Do not add facts. Do not mention formatting. Keep important URLs/domains.\n"
        "Return only the spoken text.\n\n"
        f"Message:\n{text}"
    )
    return str(agent.chat(prompt) or "").strip()


async def _maybe_summarize_text(
    text: str,
    *,
    cfg: dict[str, Any],
    config: dict[str, Any],
    summarize_fn: Callable[[str], Awaitable[str] | str] | None,
) -> str:
    try:
        if summarize_fn is None:
            summarized = await asyncio.to_thread(_default_summarize_text, text, cfg=cfg, config=config)
        else:
            maybe = summarize_fn(text)
            summarized = await maybe if inspect.isawaitable(maybe) else maybe
    except Exception as exc:
        logger.warning("Voice-summary summarization failed; using normalized text: %s", exc)
        return text

    try:
        from tools.tts_tool import normalize_text_for_tts
        cleaned = normalize_text_for_tts(str(summarized or ""))
    except Exception:
        cleaned = str(summarized or "").strip()
    return cleaned or text


def make_agent_end_handler(registry: Any) -> Callable[[str, dict[str, Any]], Awaitable[None]]:
    """Create the built-in ``agent:end`` handler bound to a hook registry."""

    async def handle_agent_end(event_type: str, context: dict[str, Any]) -> None:
        """Schedule post-delivery voice audio from the documented agent:end hook."""
        if event_type != "agent:end":
            return

        runner = getattr(registry, "gateway_runner", None)
        adapters = getattr(runner, "adapters", {}) if runner is not None else {}
        platform = context.get("platform")
        adapter = adapters.get(platform)
        if adapter is None:
            # Runner adapters are keyed by Platform enum in normal gateway use.
            try:
                from gateway.config import Platform
                adapter = adapters.get(Platform(platform))
            except Exception:
                adapter = None

        session_key = str(context.get("session_key") or "")
        register = getattr(adapter, "register_post_delivery_callback", None)
        send_voice = getattr(adapter, "send_voice", None)
        if not session_key or not callable(register) or not callable(send_voice):
            return

        text = str(context.get("response_full") or context.get("response") or "")
        if not text:
            return

        callback_context = {
            "adapter": adapter,
            "platform": platform,
            "chat_id": str(context.get("chat_id") or ""),
            "thread_id": context.get("thread_id"),
            "session_key": session_key,
            "text": text,
            "metadata": {"thread_id": context.get("thread_id")} if context.get("thread_id") else None,
        }

        def _after_delivery(*, delivery_succeeded: bool = False) -> None:
            if not delivery_succeeded:
                return
            try:
                asyncio.create_task(_send_voice_summary(callback_context))
            except RuntimeError:
                logger.debug("Voice-summary callback could not schedule async send", exc_info=True)

        try:
            register(session_key, _after_delivery)
        except Exception:
            logger.debug("Voice-summary post-delivery registration failed", exc_info=True)

    return handle_agent_end


async def _send_voice_summary(context: dict[str, Any]) -> None:
    """Generate and send the configured voice summary; never raises."""
    adapter = context.get("adapter")
    send_voice = getattr(adapter, "send_voice", None)
    if not callable(send_voice):
        return

    audio_path = await maybe_build_voice_summary(
        text=str(context.get("text") or ""),
        platform=context.get("platform"),
        chat_id=str(context.get("chat_id") or ""),
        thread_id=context.get("thread_id"),
        config=_load_runtime_config(),
        session_key=context.get("session_key"),
    )
    if not audio_path:
        return

    try:
        maybe = send_voice(
            chat_id=str(context.get("chat_id") or ""),
            audio_path=audio_path,
            metadata=context.get("metadata"),
        )
        result = await maybe if inspect.isawaitable(maybe) else maybe
        if result is not None and not getattr(result, "success", False):
            logger.warning("Voice-summary send failed: %s", getattr(result, "error", None))
    except Exception as exc:
        logger.warning("Voice-summary send failed: %s", exc, exc_info=True)
    finally:
        try:
            os.unlink(audio_path)
        except OSError:
            pass


def _load_runtime_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config
        config = load_config()
        return config if isinstance(config, dict) else {}
    except Exception as exc:
        logger.warning("Voice-summary config load failed: %s", exc)
        return {}


async def maybe_build_voice_summary(
    *,
    text: str,
    platform: Any,
    chat_id: str,
    thread_id: str | None,
    config: dict[str, Any],
    session_key: str | None,
    summarize_fn: Callable[[str], Awaitable[str] | str] | None = None,
) -> str | None:
    """Build a voice-summary audio file for a delivered text message.

    Returns the generated audio path, or ``None`` when disabled/not possible.
    Failures are logged and swallowed so visible text delivery remains the
    source of truth. The caller owns deleting the returned file after sending.
    """
    del thread_id, session_key  # reserved for future per-thread/session policy

    if not voice_summary_enabled_for(platform=platform, chat_id=chat_id, config=config):
        return None

    try:
        from tools.tts_tool import normalize_text_for_tts, text_to_speech_tool
    except Exception as exc:
        logger.warning("Voice-summary TTS unavailable: %s", exc)
        return None

    spoken = normalize_text_for_tts(text)
    if not spoken:
        return None

    cfg = _voice_summary_config(config)
    spoken = await _maybe_summarize_text(
        spoken,
        cfg=cfg,
        config=config,
        summarize_fn=summarize_fn,
    )
    spoken = spoken[: int(cfg.get("max_chars", 4000) or 4000)].strip()
    if not spoken:
        return None

    try:
        from gateway.session_context import clear_session_vars, set_session_vars
        tokens = set_session_vars(platform=_platform_name(platform), chat_id=str(chat_id or ""))
        try:
            result_json = await asyncio.to_thread(
                text_to_speech_tool,
                text=spoken,
            )
        finally:
            clear_session_vars(tokens)
        result = json.loads(result_json)
    except Exception as exc:
        logger.warning("Voice-summary TTS failed: %s", exc)
        return None

    actual_path = result.get("file_path")
    if not result.get("success") or not os.path.isfile(actual_path):
        logger.warning("Voice-summary TTS failed: %s", result.get("error") or "missing audio file")
        for path in {actual_path}:
            try:
                if path:
                    os.unlink(path)
            except OSError:
                pass
        return None

    return actual_path
