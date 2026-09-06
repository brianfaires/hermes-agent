"""Process-wide gateway restart tool.

The gateway is the unit of restart. In a multiplex deployment one process serves
all profiles, so this tool deliberately exposes no profile targeting or routing.
Profile-level authorization is handled by deciding which profiles receive the
tool.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, TimeoutError as FutureTimeout
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from gateway.restart import is_gateway_supervisor_process, is_container_restart_context

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
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


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
    in_container = is_container_restart_context()
    if under_service or in_container:
        return False, True
    return True, False


def _schedule_restart(runner: Any) -> bool:
    """Marshal the synchronous model tool onto the gateway's owning loop.

    request_restart owns after-turn drain, cron/API counts and teardown. A
    cancelled queue entry must never become a delayed surprise restart.
    """
    loop = getattr(runner, "_gateway_loop", None)
    if loop is None or not loop.is_running():
        raise RuntimeError("gateway event loop unavailable")
    detached, via_service = _restart_modes()
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if current is loop:
        return runner.request_restart(detached=detached, via_service=via_service)
    result = Future()

    def request():
        if not result.set_running_or_notify_cancel():
            return
        try:
            result.set_result(runner.request_restart(detached=detached, via_service=via_service))
        except Exception as exc:
            result.set_exception(exc)

    loop.call_soon_threadsafe(request)
    try:
        return result.result(timeout=5)
    except FutureTimeout:
        if result.cancel():
            raise RuntimeError("gateway restart scheduling timed out before execution")
        # The loop has begun the short synchronous request. Never release the
        # reservation while its outcome is unknown.
        raise RuntimeError("gateway restart scheduling outcome unknown; inspect status before retry")


def _session_authorized(runner: Any) -> bool:
    """Use existing profile opt-in and live conversation identity, never argv."""
    from gateway.session_context import get_session_env
    from hermes_cli.config import load_config_readonly

    cfg = load_config_readonly().get("plugins", {})
    if not isinstance(cfg, dict):
        return False
    enabled = cfg.get("enabled")
    if not isinstance(enabled, list) or _PLUGIN_KEY not in enabled or _PLUGIN_KEY in (cfg.get("disabled") or []):
        return False
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    key = get_session_env("HERMES_SESSION_KEY", "")
    return bool(platform and platform not in {"cli", "tui", "api", "cron"}
                and key and key in getattr(runner, "_running_agents", {})
                and not os.environ.get("HERMES_KANBAN_TASK_ID"))


def _handle_request_gateway_restart(args: dict[str, Any], **_: Any) -> str:
    runner = _resolve_runner()
    record = {"ts": time.time(), "profile": _active_profile_name(),
              "reason": str(args.get("reason") or "").strip(),
              "dry_run": args.get("dry_run") is True, "restart_scope": "all_profiles"}

    def deny(error):
        try:
            _append_audit({**record, "decision": "deny", "error": error})
        except OSError:
            pass
        return _json({"ok": False, "error": error})

    if not record["reason"]:
        return deny("missing_reason")
    if args.get("confirm") != "restart gateway":
        return deny("confirmation_required")
    if runner is None:
        return deny("gateway_runner_unavailable")
    try:
        if not _session_authorized(runner):
            return deny("gateway_session_not_authorized")
    except Exception:
        return deny("gateway_session_not_authorized")

    detached, via_service = _restart_modes()
    active = runner._active_work_count()
    result = {"ok": True, "profile": record["profile"], "restart_scope": "all_profiles",
              "active_agents": runner._running_agent_count(), "active_work": active,
              "restart_mode": {"detached": detached, "via_service": via_service},
              "audit_log": str(_audit_path())}
    try:
        if record["dry_run"]:
            _append_audit({**record, "decision": "dry_run"})
            return _json({**result, "dry_run": True, "runner_available": True})
        if runner._restart_requested:
            return _json({**result, "status": "already_in_progress"})
        if runner._draining:
            # An operator's stop/update drain is not permission to restart it.
            return deny("gateway_already_draining")
        now = record["ts"]
        remaining = _reserve_restart(now, _coerce_int(_plugin_config().get("cooldown_seconds"), _DEFAULT_COOLDOWN_SECONDS))
        if remaining:
            return deny("cooldown_active")
        try:
            _append_audit({**record, "decision": "reserved", "active_work": active})
        except OSError:
            _release_restart_reservation(now)
            return deny("audit_unavailable")
        try:
            scheduled = _schedule_restart(runner)
        except Exception as exc:
            # Keep reservation on an ambiguous handoff; safe retries remain
            # bounded by the existing shared cooldown.
            return deny("schedule_failed: " + str(exc))
        _append_audit({**record, "decision": "scheduled" if scheduled else "already_in_progress", "active_work": active})
        return _json({**result, "status": ("restart_draining" if active else "restart_scheduled") if scheduled else "already_in_progress"})
    except OSError:
        return deny("audit_or_state_unavailable")


def _check_available() -> bool:
    # Process reachability only; profile/session authorization is checked at use.
    return _resolve_runner() is not None


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
