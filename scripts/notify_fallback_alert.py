#!/usr/bin/env python3
"""Shell-hook consumer for high-priority fallback activation alerts.

Reads the ``fallback_activated`` hook payload on stdin and sends one bounded
alert to every configured default-profile messaging home channel.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX production path has fcntl.
    fcntl = None  # type: ignore[assignment]


PREFIX = "🚨 High-priority Alert 🚨"
MAX_CHARS = 140
COOLDOWN_SECONDS = 300
SEND_TIMEOUT_SECONDS = 60
STATE_RELATIVE_PATH = Path("state") / "fallback-alert-cooldown.json"
IGNORED_HOME_PLATFORMS = {
    "api_server",
    "homeassistant",
    "local",
    "msgraph_webhook",
    "webhook",
}
PRESERVE_ENV = {
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NO_PROXY",
    "PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USER",
    "VIRTUAL_ENV",
}


def _read_payload() -> dict[str, Any]:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid hook JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("hook payload must be a JSON object")
    return data


def _extra(payload: dict[str, Any]) -> dict[str, Any]:
    extra = payload.get("extra")
    return extra if isinstance(extra, dict) else {}


def _default_root() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root()


def _default_env(root: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in PRESERVE_ENV and isinstance(value, str)
    }
    env["HERMES_HOME"] = str(root)
    env["HERMES_PROFILE"] = "default"
    env.pop("HERMES_SESSION_PLATFORM", None)
    env.pop("HERMES_SESSION_USER_ID", None)
    env.pop("HERMES_SESSION_USER_NAME", None)
    env.pop("HERMES_SESSION_CHAT_ID", None)
    env.pop("HERMES_SESSION_THREAD_ID", None)
    return env


@contextmanager
def _scoped_default_env(root: Path) -> Iterator[None]:
    env = _default_env(root)
    old = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


def _home_platforms(root: Path) -> list[str]:
    with _scoped_default_env(root):
        # Reuse the same env/config bridge as `hermes send`, then the gateway
        # loader so plugin-owned platform bridges (for example ntfy/teams/irc)
        # can seed their home_channel entries under the default identity.
        from hermes_cli.send_cmd import _load_hermes_env
        from gateway.config import load_gateway_config

        _load_hermes_env()
        config = load_gateway_config()
        platforms = []
        for platform, platform_config in config.platforms.items():
            name = str(getattr(platform, "value", platform)).strip().lower()
            if not name or name in IGNORED_HOME_PLATFORMS:
                continue
            if not getattr(platform_config, "enabled", False):
                continue
            if getattr(platform_config, "home_channel", None) is None:
                continue
            platforms.append(name)
    return sorted(set(platforms))


def _message(extra: dict[str, Any]) -> str:
    provider = str(
        extra.get("fallback_provider") or extra.get("provider") or "unknown"
    ).strip() or "unknown"
    model = str(extra.get("fallback_model") or extra.get("model") or "unknown").strip()
    suffix = f" Fallback active: {provider}"
    if model:
        suffix += f"/{model}"
    available = MAX_CHARS - len(PREFIX)
    if len(suffix) > available:
        suffix = suffix[: max(0, available - 3)].rstrip() + "..."
    message = PREFIX + suffix
    return message[:MAX_CHARS]


@contextmanager
def _cooldown_lock(root: Path) -> Iterator[Path]:
    if fcntl is None:
        raise RuntimeError("fallback alert cooldown locking requires fcntl")
    state_path = root / STATE_RELATIVE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield state_path
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, data: dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass


def _reserve_cooldown(root: Path, extra: dict[str, Any]) -> bool:
    now = time.time()
    with _cooldown_lock(root) as state_path:
        state = _read_state(state_path)
        try:
            until = float(state.get("until_epoch", 0) or 0)
        except (TypeError, ValueError):
            until = 0
        if until > now:
            return False
        _write_state(
            state_path,
            {
                "reserved_at_epoch": now,
                "until_epoch": now + COOLDOWN_SECONDS,
                "profile_name": str(extra.get("profile_name") or ""),
                "provider": str(extra.get("fallback_provider") or extra.get("provider") or ""),
                "model": str(extra.get("fallback_model") or extra.get("model") or ""),
                "stage": str(extra.get("stage") or ""),
            },
        )
        return True


def _run_send(
    hermes_bin: str,
    *,
    root: Path,
    platform: str,
    message: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [hermes_bin, "send", "--to", platform, "--json", message],
        env=_default_env(root),
        text=True,
        capture_output=True,
        timeout=SEND_TIMEOUT_SECONDS,
        check=False,
    )


def _send_failure(platform: str, result: subprocess.CompletedProcess[str]) -> str | None:
    detail = (result.stderr or result.stdout or "").strip()
    if result.returncode != 0:
        return f"{platform}: exit {result.returncode} {detail}".strip()
    if not result.stdout.strip():
        return f"{platform}: empty JSON result"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return f"{platform}: invalid JSON result {exc}"
    if not isinstance(payload, dict):
        return f"{platform}: JSON result was not an object"
    if payload.get("error"):
        return f"{platform}: {payload['error']}"
    if payload.get("skipped"):
        return f"{platform}: delivery skipped"
    if payload.get("success") is True:
        return None
    return f"{platform}: delivery not confirmed"


def _deliver_all(
    hermes_bin: str,
    *,
    root: Path,
    platforms: list[str],
    message: str,
) -> list[str]:
    failures: list[str] = []

    def deliver(platform: str) -> str | None:
        try:
            result = _run_send(
                hermes_bin,
                root=root,
                platform=platform,
                message=message,
            )
        except subprocess.TimeoutExpired:
            return f"{platform}: timeout"
        except Exception as exc:
            return f"{platform}: {exc}"
        return _send_failure(platform, result)

    with ThreadPoolExecutor(max_workers=len(platforms)) as executor:
        futures = {executor.submit(deliver, platform): platform for platform in platforms}
        for future in as_completed(futures):
            failure = future.result()
            if failure:
                failures.append(failure)

    return failures


def main() -> int:
    payload = _read_payload()
    if payload.get("hook_event_name") != "fallback_activated":
        return 0

    extra = _extra(payload)
    root = _default_root()
    platforms = _home_platforms(root)
    if not platforms:
        print(
            "fallback alert: no enabled default-profile messaging home channels",
            file=sys.stderr,
        )
        return 1

    if not _reserve_cooldown(root, extra):
        print("fallback alert: suppressed by global cooldown", file=sys.stderr)
        return 0

    hermes_bin = shutil.which("hermes", path=os.environ.get("PATH"))
    if not hermes_bin:
        print("fallback alert: hermes executable not found on PATH", file=sys.stderr)
        return 1

    message = _message(extra)
    failures = _deliver_all(
        hermes_bin,
        root=root,
        platforms=platforms,
        message=message,
    )

    if failures:
        print("fallback alert delivery failed: " + "; ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"fallback alert: {exc}", file=sys.stderr)
        raise SystemExit(1)
