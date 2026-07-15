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
import subprocess
import sys
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
_DEFAULT_SCHEDULE_DELAY_SECONDS = 3.0
_cooldown_lock = threading.Lock()

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
            "target_profile": {
                "type": "string",
                "description": (
                    "Profile gateway to restart. Defaults to the invoking profile. "
                    "A different profile must be listed in allowed_target_profiles."
                ),
            },
            "target_profiles": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Profile gateways to restart as one validated batch. Mutually exclusive with "
                    "target_profile. Every target must be listed in allowed_target_profiles."
                ),
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


def _normalize_profile_name(value: str) -> str:
    from hermes_cli import profiles as profiles_mod

    normalized = profiles_mod.normalize_profile_name(value)
    profiles_mod.validate_profile_name(normalized)
    return normalized


def _allowed_target_profiles(cfg: dict[str, Any], source_profile: str) -> set[str]:
    """Return explicitly configured cross-profile targets plus the source."""
    configured = cfg.get("allowed_target_profiles", [])
    if isinstance(configured, str):
        configured = configured.split(",")
    if not isinstance(configured, (list, tuple, set, frozenset)):
        configured = []

    targets = {source_profile}
    for item in configured:
        try:
            targets.add(_normalize_profile_name(str(item).strip()))
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid allowed_target_profiles entry")
    return targets


def _audit_path() -> Path:
    return _hermes_home() / "logs" / "gateway-restart-tool.jsonl"


def _state_path() -> Path:
    return _hermes_home() / ".gateway_restart_tool_state.json"


@contextmanager
def _restart_state_lock():
    """Serialize restart-state read/modify/write across threads and processes."""
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


def _read_last_restart_times() -> tuple[dict[str, float], float]:
    """Return target-profile cooldowns plus an unscoped legacy timestamp."""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        raw_times = data.get("last_requested_at_by_profile")
        times = (
            {str(profile): float(timestamp) for profile, timestamp in raw_times.items()}
            if isinstance(raw_times, dict)
            else {}
        )
        return times, float(data.get("last_requested_at") or 0.0)
    except Exception:
        return {}, 0.0


def _read_last_restart_time(target_profile: str, source_profile: str) -> float:
    times, legacy = _read_last_restart_times()
    if target_profile in times:
        return times[target_profile]
    # Old state had no target identity, so retain its conservative global gate
    # until the first scoped write replaces it.  This avoids an immediate
    # duplicate restart of a formerly remote target during upgrade.
    return legacy if not times else 0.0


def _write_last_restart_time(target_profile: str, now: float) -> None:
    path = _state_path()
    times, _legacy = _read_last_restart_times()
    times[target_profile] = now
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"last_requested_at_by_profile": times}, sort_keys=True), encoding="utf-8"
    )
    tmp.replace(path)


def _reserve_restart(
    target_profile: str,
    source_profile: str,
    now: float,
    cooldown_seconds: int,
) -> int:
    """Atomically check and reserve one target's cooldown across processes."""
    with _restart_state_lock():
        last = _read_last_restart_time(target_profile, source_profile)
        remaining = max(0, int(cooldown_seconds - (now - last)))
        if remaining:
            return remaining
        _write_last_restart_time(target_profile, now)
        return 0


def _release_restart_reservation(target_profile: str, reserved_at: float) -> None:
    """Release this request's reservation without removing a newer one."""
    with _restart_state_lock():
        times, _legacy = _read_last_restart_times()
        if times.get(target_profile) != reserved_at:
            return
        del times[target_profile]
        path = _state_path()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"last_requested_at_by_profile": times}, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(path)


def _target_profiles(args: dict[str, Any], source_profile: str) -> list[str]:
    """Normalize one target or a deduplicated batch of targets."""
    singular = args.get("target_profile")
    plural = args.get("target_profiles")
    if singular is not None and plural is not None:
        raise ValueError("target_profile and target_profiles are mutually exclusive")
    raw_targets = [singular or source_profile] if plural is None else plural
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("target_profiles must be a non-empty array")
    targets: list[str] = []
    for raw_target in raw_targets:
        target = _normalize_profile_name(str(raw_target).strip())
        if target not in targets:
            targets.append(target)
    return targets


def _resolve_runner() -> Any | None:
    try:
        from gateway import run as gateway_run

        ref = getattr(gateway_run, "_gateway_runner_ref", None)
        return ref() if callable(ref) else None
    except Exception:
        return None


def _restart_modes() -> tuple[bool, bool]:
    under_service = is_gateway_supervisor_process()
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


def _spawn_profile_restart(target_profile: str) -> int:
    """Run the normal per-profile gateway restart command in a detached child."""
    command = [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "-p",
        target_profile,
        "gateway",
        "restart",
    ]
    environment = {**os.environ, "HERMES_NONINTERACTIVE": "1"}
    # This detached child controls a *different* profile. The CLI's gateway
    # marker protects against self-restart loops, so it must not leak here.
    environment.pop("_HERMES_GATEWAY", None)
    kwargs: dict[str, Any] = {
        "cwd": str(Path(__file__).resolve().parents[2]),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": environment,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, **kwargs).pid


def _handle_request_gateway_restart(args: dict[str, Any], **_: Any) -> str:
    cfg = _plugin_config()
    try:
        profile = _normalize_profile_name(_active_profile_name())
        target_profiles = _target_profiles(args, profile)
    except (TypeError, ValueError):
        error = "invalid_target_profiles" if args.get("target_profiles") is not None else "invalid_target_profile"
        return _json({"ok": False, "error": error})
    target_profile = target_profiles[0]
    allowed_targets = _allowed_target_profiles(cfg, profile)
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
        "target_profiles": target_profiles,
        "reason": reason,
        "dry_run": dry_run,
    }

    forbidden_targets = sorted(set(target_profiles) - allowed_targets)
    if forbidden_targets:
        record.update({"decision": "deny", "error": "target_profile_not_allowed"})
        _append_audit(record)
        if len(target_profiles) == 1:
            return _json(
                {
                    "ok": False,
                    "error": "target_profile_not_allowed",
                    "profile": profile,
                    "target_profile": target_profile,
                    "allowed_target_profiles": sorted(allowed_targets),
                }
            )
        return _json(
            {
                "ok": False,
                "error": "target_profile_not_allowed",
                "profile": profile,
                "target_profiles": target_profiles,
                "forbidden_target_profiles": forbidden_targets,
                "allowed_target_profiles": sorted(allowed_targets),
            }
        )

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

    if len(target_profiles) > 1:
        cooldowns = {
            target: max(
                0,
                int(cooldown_seconds - (now - _read_last_restart_time(target, profile))),
            )
            for target in target_profiles
        }
        active_cooldowns = {target: remaining for target, remaining in cooldowns.items() if remaining}
        if active_cooldowns and not dry_run:
            record.update(
                {
                    "decision": "deny",
                    "error": "cooldown_active",
                    "cooldown_active_profiles": active_cooldowns,
                }
            )
            _append_audit(record)
            return _json(
                {
                    "ok": False,
                    "error": "cooldown_active",
                    "cooldown_active_profiles": active_cooldowns,
                }
            )
        # Restart remote targets first. Scheduling this gateway drains the
        # current agent, so it must be the last operation in a batch.
        ordered_targets = [target for target in target_profiles if target != profile]
        if profile in target_profiles:
            ordered_targets.append(profile)
        results = []
        for target in ordered_targets:
            per_target_args = {key: value for key, value in args.items() if key != "target_profiles"}
            per_target_args["target_profile"] = target
            results.append(json.loads(_handle_request_gateway_restart(per_target_args)))
        return _json(
            {
                "ok": all(result.get("ok") for result in results),
                "status": "restart_batch_scheduled",
                "profile": profile,
                "target_profiles": ordered_targets,
                "restarts": results,
            }
        )

    is_local_target = target_profile == profile
    runner = _resolve_runner() if is_local_target else None
    runner_available = runner is not None
    active_agents = None
    if runner_available:
        try:
            active_agents = int(runner._running_agent_count())
        except Exception:
            active_agents = None

    detached, via_service = _restart_modes()
    dispatch = "in_process" if is_local_target else "profile_cli"
    if dry_run:
        record.update(
            {
                "decision": "dry_run",
                "runner_available": runner_available,
                "dispatch": dispatch,
            }
        )
        _append_audit(record)
        return _json(
            {
                "ok": True,
                "dry_run": True,
                "profile": profile,
                "target_profile": target_profile,
                "dispatch": dispatch,
                "runner_available": runner_available,
                "active_agents": active_agents,
                "would_schedule_after_seconds": delay_seconds,
                "restart_mode": {"detached": detached, "via_service": via_service},
                "audit_log": str(_audit_path()),
            }
        )

    if is_local_target and runner is None:
        record.update({"decision": "deny", "error": "gateway_runner_unavailable"})
        _append_audit(record)
        return _json(
            {
                "ok": False,
                "error": "gateway_runner_unavailable",
                "detail": "This tool must run inside the live gateway process.",
            }
        )

    if is_local_target and (
        getattr(runner, "_restart_requested", False) or getattr(runner, "_draining", False)
    ):
        record.update({"decision": "already_in_progress", "active_agents": active_agents})
        _append_audit(record)
        return _json({"ok": True, "status": "already_in_progress", "active_agents": active_agents})

    cooldown_remaining = _reserve_restart(target_profile, profile, now, cooldown_seconds)
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

    if not is_local_target:
        try:
            child_pid = _spawn_profile_restart(target_profile)
        except OSError as exc:
            _release_restart_reservation(target_profile, now)
            record.update({"decision": "failed", "error": "profile_restart_spawn_failed"})
            _append_audit(record)
            return _json(
                {"ok": False, "error": "profile_restart_spawn_failed", "detail": str(exc)}
            )
        record.update({"decision": "scheduled", "dispatch": dispatch, "child_pid": child_pid})
        _append_audit(record)
        return _json(
            {
                "ok": True,
                "status": "restart_scheduled",
                "profile": profile,
                "target_profile": target_profile,
                "dispatch": dispatch,
                "child_pid": child_pid,
                "audit_log": str(_audit_path()),
            }
        )

    schedule_error = None
    try:
        scheduled = _schedule_restart(runner, delay_seconds)
    except Exception as exc:
        logger.exception("gateway restart scheduling failed for %s", target_profile)
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
        _release_restart_reservation(target_profile, now)
        result = {"ok": False, "error": "schedule_failed"}
        if schedule_error:
            result["detail"] = schedule_error
        return _json(result)
    return _json(
        {
            "ok": True,
            "status": "restart_scheduled",
            "profile": profile,
            "target_profile": target_profile,
            "dispatch": dispatch,
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
