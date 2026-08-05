"""Process-wide gateway restart tool.

The gateway is the unit of restart. In a multiplex deployment one process serves
all profiles, so this tool deliberately exposes no profile targeting or routing.
Profile-level authorization is handled by deciding which profiles receive the
tool.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from gateway.restart import is_gateway_supervisor_process

logger = logging.getLogger(__name__)

_TOOL_NAME = "request_gateway_restart"
_TOOLSET = "gateway_restart"
_PLUGIN_KEY = "gateway-restart-tool"
_DEFAULT_COOLDOWN_SECONDS = 300
_cooldown_lock = threading.Lock()

REQUEST_GATEWAY_RESTART_SCHEMA = {
    "name": _TOOL_NAME,
    "description": "Restart the shared Hermes gateway for all profiles.",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "Operational reason for the restart. Required and written "
                    "to the audit log."
                ),
            },
            "confirm": {
                "type": "string",
                "description": "Must be exactly 'restart gateway' for a real restart.",
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "If true, validate policy and report what would happen "
                    "without restarting."
                ),
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


def _resolve_runner() -> Any | None:
    try:
        from gateway import run as gateway_run

        ref = getattr(gateway_run, "_gateway_runner_ref", None)
        return ref() if callable(ref) else None
    except Exception:
        return None


def _restart_storage_home() -> Path:
    runner = _resolve_runner()
    owner_home = getattr(runner, "_gateway_profile_home", None)
    return Path(owner_home) if owner_home is not None else _hermes_home()


def _audit_path() -> Path:
    return _restart_storage_home() / "logs" / "gateway-restart-tool.jsonl"


def _state_path() -> Path:
    return _restart_storage_home() / ".gateway_restart_tool_state.json"


@contextmanager
def _restart_state_lock():
    """Serialize restart cooldown state across threads and processes."""
    with _cooldown_lock:
        state_path = _state_path()
        lock_path = state_path.with_suffix(state_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows branch
            fcntl = None
        try:
            import msvcrt
        except ImportError:  # pragma: no cover - POSIX branch
            msvcrt = None

        if fcntl is None and msvcrt is None:
            yield
            return
        if msvcrt is not None and (
            not lock_path.exists() or lock_path.stat().st_size == 0
        ):
            lock_path.write_text(" ", encoding="utf-8")
        lock_file = open(
            lock_path,
            "r+" if msvcrt is not None else "a+",
            encoding="utf-8",
        )
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            else:
                assert msvcrt is not None
                lock_file.seek(0)
                getattr(msvcrt, "locking")(
                    lock_file.fileno(), getattr(msvcrt, "LK_LOCK"), 1
                )
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                else:
                    assert msvcrt is not None
                    lock_file.seek(0)
                    getattr(msvcrt, "locking")(
                        lock_file.fileno(), getattr(msvcrt, "LK_UNLCK"), 1
                    )
            finally:
                lock_file.close()


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
        if data.get("last_requested_at") is not None:
            return float(data["last_requested_at"])
        legacy = data.get("last_requested_at_by_profile")
        if isinstance(legacy, dict) and legacy:
            return max(float(value) for value in legacy.values())
    except Exception:
        pass
    return 0.0


def _write_last_restart_time(now: float) -> None:
    path = _state_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"last_requested_at": now}), encoding="utf-8")
    tmp.replace(path)


def _reserve_restart(now: float, cooldown_seconds: int) -> int:
    with _restart_state_lock():
        remaining = max(0, int(cooldown_seconds - (now - _read_last_restart_time())))
        if remaining:
            return remaining
        _write_last_restart_time(now)
        return 0


def _release_restart_reservation(reserved_at: float) -> None:
    with _restart_state_lock():
        if _read_last_restart_time() != reserved_at:
            return
        _state_path().unlink(missing_ok=True)


def _restart_modes() -> tuple[bool, bool]:
    under_service = is_gateway_supervisor_process()
    in_container = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
    if under_service or in_container:
        return False, True
    return True, False


def _schedule_restart(
    runner: Any,
    _delay_seconds: float,
    *,
    defer_until_session_delivered: str | None = None,
) -> bool:
    detached, via_service = _restart_modes()

    kwargs = {"detached": detached, "via_service": via_service}
    if defer_until_session_delivered:
        kwargs["defer_until_session_delivered"] = defer_until_session_delivered
    runner.request_restart(**kwargs)
    return True


def _handle_request_gateway_restart(args: dict[str, Any], **_: Any) -> str:
    cfg = _plugin_config()
    cooldown_seconds = _coerce_int(
        cfg.get("cooldown_seconds"), _DEFAULT_COOLDOWN_SECONDS, minimum=0
    )
    delay_seconds = 0.0
    reason = str(args.get("reason") or "").strip()
    confirm = str(args.get("confirm") or "")
    dry_run = args.get("dry_run") is True
    caller_session_key = None
    try:
        from gateway.session_context import get_session_env

        caller_session_key = (
            str(get_session_env("HERMES_SESSION_KEY", "") or "").strip() or None
        )
    except Exception:
        caller_session_key = None
    profile = _active_profile_name()
    now = time.time()
    runner = _resolve_runner()
    active_agents = None
    if runner is not None:
        try:
            active_agents = int(runner._running_agent_count())
        except Exception:
            active_agents = None

    record = {
        "ts": now,
        "profile": profile,
        "reason": reason,
        "dry_run": dry_run,
        "restart_scope": "all_profiles",
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

    detached, via_service = _restart_modes()
    if dry_run:
        record.update({"decision": "dry_run", "runner_available": runner is not None})
        _append_audit(record)
        return _json(
            {
                "ok": True,
                "dry_run": True,
                "profile": profile,
                "restart_scope": "all_profiles",
                "runner_available": runner is not None,
                "active_agents": active_agents,
                "would_schedule_after_seconds": delay_seconds,
                "restart_mode": {"detached": detached, "via_service": via_service},
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

    if getattr(runner, "_restart_requested", False) or getattr(
        runner, "_draining", False
    ):
        record.update({"decision": "already_in_progress", "active_agents": active_agents})
        _append_audit(record)
        return _json(
            {
                "ok": True,
                "status": "already_in_progress",
                "restart_scope": "all_profiles",
                "active_agents": active_agents,
            }
        )

    cooldown_remaining = _reserve_restart(now, cooldown_seconds)
    if cooldown_remaining:
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

    schedule_error = None
    try:
        scheduled = _schedule_restart(
            runner,
            delay_seconds,
            defer_until_session_delivered=caller_session_key,
        )
    except Exception as exc:
        logger.exception("shared gateway restart scheduling failed")
        scheduled = False
        schedule_error = str(exc)

    record.update(
        {
            "decision": "scheduled" if scheduled else "failed",
            "active_agents": active_agents,
            "delay_seconds": delay_seconds,
            "detached": detached,
            "via_service": via_service,
        }
    )
    if schedule_error:
        record.update({"error": "schedule_failed", "detail": schedule_error})
    _append_audit(record)

    if not scheduled:
        _release_restart_reservation(now)
        result = {"ok": False, "error": "schedule_failed"}
        if schedule_error:
            result["detail"] = schedule_error
        return _json(result)

    return _json(
        {
            "ok": True,
            "status": "restart_draining"
            if active_agents is not None and active_agents > 0
            else "restart_scheduled",
            "profile": profile,
            "restart_scope": "all_profiles",
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
