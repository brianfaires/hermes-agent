"""Gateway restart tool plugin.

This is intentionally profile-local user-plugin code. It does not patch Hermes
core. The tool exposes one narrow operation: schedule the live GatewayRunner's
existing graceful restart path after a short delay so the agent can return its
final acknowledgement before the gateway drains and exits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TOOL_NAME = "request_gateway_restart"
_TOOLSET = "gateway_restart"
_PLUGIN_KEY = "gateway-restart-tool"
_DEFAULT_COOLDOWN_SECONDS = 300
_DEFAULT_SCHEDULE_DELAY_SECONDS = 3.0

REQUEST_GATEWAY_RESTART_SCHEMA = {
    "name": _TOOL_NAME,
    "description": (
        "Request a graceful restart of the live Hermes messaging gateway. "
        "Use only when a gateway restart is operationally necessary, after "
        "capturing a clear reason. The tool is audited, cooldown-limited, "
        "and reuses the gateway's existing drain/restart path."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Operational reason for the restart. Required and written to the audit log.",
            },
            "confirm": {
                "type": "string",
                "description": "Must be exactly 'restart gateway' for a real restart.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, validate policy and report what would happen without restarting.",
                "default": False,
            },
        },
        "required": ["reason", "confirm"],
        "additionalProperties": False,
    },
}


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True)


def _plugin_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception:
        return {}
    plugins = config.get("plugins") if isinstance(config, dict) else None
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    entry = entries.get(_PLUGIN_KEY) if isinstance(entries, dict) else None
    return entry if isinstance(entry, dict) else {}


def _active_profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name()
    except Exception:
        return os.getenv("HERMES_PROFILE", "") or "unknown"


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _coerce_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _coerce_float(value: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _audit_path() -> Path:
    return _hermes_home() / "logs" / "gateway-restart-tool.jsonl"


def _state_path() -> Path:
    return _hermes_home() / ".gateway_restart_tool_state.json"


def _append_audit(record: dict[str, Any]) -> None:
    try:
        path = _audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception as exc:  # pragma: no cover - audit must not crash policy path
        logger.debug("gateway restart tool audit write failed: %s", exc)


def _read_last_restart_time() -> float:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return float(data.get("last_requested_at") or 0.0)
    except Exception:
        return 0.0


def _write_last_restart_time(now: float) -> None:
    path = _state_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"last_requested_at": now}, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _resolve_runner() -> Any | None:
    try:
        from gateway import run as gateway_run

        ref = getattr(gateway_run, "_gateway_runner_ref", None)
        return ref() if callable(ref) else None
    except Exception:
        return None


def _restart_modes() -> tuple[bool, bool]:
    under_service = bool(os.environ.get("INVOCATION_ID"))
    in_container = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
    if under_service or in_container:
        return False, True
    return True, False


def _schedule_restart(runner: Any, delay_seconds: float) -> bool:
    detached, via_service = _restart_modes()

    async def _delayed_restart() -> None:
        await asyncio.sleep(delay_seconds)
        runner.request_restart(detached=detached, via_service=via_service)

    loop = getattr(runner, "_gateway_loop", None)
    if loop is not None and getattr(loop, "is_running", lambda: False)():
        asyncio.run_coroutine_threadsafe(_delayed_restart(), loop)
        return True

    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        # Last-resort path. This should not normally be used from gateway tool
        # execution, but preserves behavior in unusual embedded runtimes.
        runner.request_restart(detached=detached, via_service=via_service)
        return True

    running_loop.create_task(_delayed_restart())
    return True


def _handle_request_gateway_restart(args: dict[str, Any], **_: Any) -> str:
    cfg = _plugin_config()
    profile = _active_profile_name()
    cooldown_seconds = _coerce_int(
        cfg.get("cooldown_seconds"), _DEFAULT_COOLDOWN_SECONDS, minimum=0
    )
    delay_seconds = _coerce_float(
        cfg.get("schedule_delay_seconds"), _DEFAULT_SCHEDULE_DELAY_SECONDS, minimum=0.5
    )
    reason = str(args.get("reason") or "").strip()
    confirm = str(args.get("confirm") or "").strip().lower()
    dry_run = bool(args.get("dry_run"))
    now = time.time()

    record = {
        "ts": now,
        "profile": profile,
        "reason": reason,
        "dry_run": dry_run,
    }

    if not reason:
        record.update({"decision": "deny", "error": "missing_reason"})
        _append_audit(record)
        return _json({"ok": False, "error": "missing_reason"})

    if confirm != "restart gateway":
        record.update({"decision": "deny", "error": "confirmation_required"})
        _append_audit(record)
        return _json(
            {
                "ok": False,
                "error": "confirmation_required",
                "required_confirm": "restart gateway",
            }
        )

    last = _read_last_restart_time()
    cooldown_remaining = max(0, int(cooldown_seconds - (now - last)))
    if cooldown_remaining and not dry_run:
        record.update(
            {
                "decision": "deny",
                "error": "cooldown_active",
                "cooldown_remaining_seconds": cooldown_remaining,
            }
        )
        _append_audit(record)
        return _json(
            {
                "ok": False,
                "error": "cooldown_active",
                "cooldown_remaining_seconds": cooldown_remaining,
            }
        )

    runner = _resolve_runner()
    runner_available = runner is not None
    active_agents = None
    if runner_available:
        try:
            active_agents = int(runner._running_agent_count())
        except Exception:
            active_agents = None

    detached, via_service = _restart_modes()
    if dry_run:
        record.update({"decision": "dry_run", "runner_available": runner_available})
        _append_audit(record)
        return _json(
            {
                "ok": True,
                "dry_run": True,
                "profile": profile,
                "runner_available": runner_available,
                "active_agents": active_agents,
                "would_schedule_after_seconds": delay_seconds,
                "restart_mode": {
                    "detached": detached,
                    "via_service": via_service,
                },
                "audit_log": str(_audit_path()),
            }
        )

    if runner is None:
        record.update({"decision": "deny", "error": "gateway_runner_unavailable"})
        _append_audit(record)
        return _json(
            {
                "ok": False,
                "error": "gateway_runner_unavailable",
                "detail": "This tool must run inside the live gateway process.",
            }
        )

    if getattr(runner, "_restart_requested", False) or getattr(runner, "_draining", False):
        record.update({"decision": "already_in_progress", "active_agents": active_agents})
        _append_audit(record)
        return _json(
            {
                "ok": True,
                "status": "already_in_progress",
                "active_agents": active_agents,
            }
        )

    _write_last_restart_time(now)
    scheduled = _schedule_restart(runner, delay_seconds)
    record.update(
        {
            "decision": "scheduled" if scheduled else "failed",
            "active_agents": active_agents,
            "delay_seconds": delay_seconds,
            "detached": detached,
            "via_service": via_service,
        }
    )
    _append_audit(record)
    if not scheduled:
        return _json({"ok": False, "error": "schedule_failed"})
    return _json(
        {
            "ok": True,
            "status": "restart_scheduled",
            "scheduled_after_seconds": delay_seconds,
            "active_agents": active_agents,
            "restart_mode": {"detached": detached, "via_service": via_service},
            "audit_log": str(_audit_path()),
        }
    )


def _check_available() -> bool:
    return True


def register(ctx) -> None:
    ctx.register_tool(
        name=_TOOL_NAME,
        toolset=_TOOLSET,
        schema=REQUEST_GATEWAY_RESTART_SCHEMA,
        handler=_handle_request_gateway_restart,
        check_fn=_check_available,
        description=REQUEST_GATEWAY_RESTART_SCHEMA["description"],
        emoji="♻️",
    )
