#!/usr/bin/env python3
"""Bootstrap-only runtime evidence producers for legacy drain/recovery health."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
import time

from controller import loads, proc, require, shape, show
from drain_proof import COVERAGE, _read_hold


def _query_socket(home: Path, verb: str, timeout: float = 2.0) -> dict:
    request = json.dumps({"verb": verb, "id": 1, "protocol": 1}).encode() + b"\n"
    deadline = time.monotonic() + timeout
    last_error = "gateway control socket missing"
    while time.monotonic() < deadline:
        socket_path = home / "gateway.sock"
        pointer = home / "gateway.sock.path"
        if not socket_path.exists() and pointer.is_file():
            socket_path = Path(pointer.read_text(encoding="utf-8").strip())
        if not socket_path.exists():
            time.sleep(0.05)
            continue
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(max(0.05, deadline - time.monotonic()))
                client.connect(str(socket_path))
                client.sendall(request)
                raw = client.recv(512 * 1024)
            line, _, _ = raw.partition(b"\n")
            response = loads(line)
            require(response.get("ok") is True and type(response.get("result")) is dict,
                    "gateway control socket refused")
            return response["result"]
        except Exception as exc:
            last_error = type(exc).__name__
            time.sleep(0.05)
    raise RuntimeError(last_error)


def _write_legacy_drain_request(home: Path) -> None:
    payload = {
        "action": "drain",
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "principal": "release-bootstrap",
        "suppress_notification": True,
    }
    temp = home / ".drain_request.json.tmp"
    final = home / ".drain_request.json"
    with temp.open("w", encoding="utf-8") as file:
        json.dump(payload, file, sort_keys=True)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, final)


def clear_legacy_drain_request(home: Path) -> dict:
    home = home.resolve(strict=True)
    path = home / ".drain_request.json"
    try:
        payload = loads(path.read_bytes())
    except FileNotFoundError:
        return {"cleared": False, "reason": "absent", "observed": int(time.time())}
    require(type(payload) is dict, "drain marker malformed")
    require(payload.get("action") == "drain"
            and payload.get("principal") == "release-bootstrap",
            "drain marker not owned by release bootstrap")
    path.unlink()
    return {"cleared": True, "observed": int(time.time())}


def _active_agents_zero(status: dict) -> bool:
    value = status.get("active_agents")
    require(type(value) is int, "active work count missing")
    return value == 0


def prove_legacy_drain(home: Path, hold_path: Path, repo: str, unit: str, pid: int,
                       starttime: str, *, max_age: int, repeat_delay: float,
                       recovery_deadline: int, observe_timeout: float) -> dict:
    home = home.resolve(strict=True)
    _read_hold(hold_path, repo=repo, unit=unit, pid=pid, starttime=starttime,
               max_age=max_age, recovery_deadline=recovery_deadline)
    _write_legacy_drain_request(home)
    deadline = time.monotonic() + observe_timeout
    observations = []
    while time.monotonic() < deadline and len(observations) < 2:
        status = _query_socket(home, "status")
        if (status.get("pid") == pid and status.get("answering_pid") == pid
                and str(status.get("start_time")) == starttime
                and status.get("gateway_state") == "draining"
                and _active_agents_zero(status)):
            observations.append(status)
            if len(observations) < 2:
                time.sleep(repeat_delay)
        else:
            time.sleep(0.1)
    require(len(observations) == 2, "legacy drain observation missing")
    _read_hold(hold_path, repo=repo, unit=unit, pid=pid, starttime=starttime,
               max_age=max_age, recovery_deadline=recovery_deadline)
    return {"active_jobs": 0, "unit": unit, "pid": pid, "starttime": starttime,
            "observed": int(time.time())}


def prove_legacy_health(home: Path, unit: str, expected_argv: list[str],
                        expected_source: str, mode: str, *, max_age: int) -> dict:
    home = home.resolve(strict=True)
    require(mode == "legacy", "unsupported bootstrap recovery mode")
    status = show(unit)
    require(status["ActiveState"] == "active", "service inactive")
    actual = proc(int(status["MainPID"]))
    require(actual["argv"] == expected_argv, "runtime argv mismatch")
    identify = _query_socket(home, "identify")
    runtime = _query_socket(home, "status")
    require(identify.get("pid") == actual["pid"] and runtime.get("pid") == actual["pid"]
            and runtime.get("answering_pid") == actual["pid"], "gateway process mismatch")
    require(str(identify.get("start_time")) == actual["starttime"]
            and str(runtime.get("start_time")) == actual["starttime"], "gateway starttime mismatch")
    require(runtime.get("gateway_state") == "running", "gateway not running")
    answered = runtime.get("answered_at")
    require(type(answered) in (int, float) and 0 <= time.time() - answered <= max_age,
            "gateway health observation stale")
    return {"healthy": True, "unit": unit, "pid": actual["pid"],
            "starttime": actual["starttime"], "argv": actual["argv"],
            "source": expected_source, "mode": mode, "observed": int(time.time())}


def _argv_json(value: str) -> list[str]:
    parsed = loads(value.encode())
    shape(parsed, [str], "argv")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    drain = sub.add_parser("legacy-drain")
    drain.add_argument("hermes_home", type=Path)
    drain.add_argument("hold_json", type=Path)
    drain.add_argument("repo")
    drain.add_argument("unit")
    drain.add_argument("pid", type=int)
    drain.add_argument("starttime")
    drain.add_argument("--recovery-deadline", type=int, required=True)
    drain.add_argument("--max-age", type=int, default=30)
    drain.add_argument("--repeat-delay", type=float, default=0.2)
    drain.add_argument("--observe-timeout", type=float, default=10.0)

    clear = sub.add_parser("clear-drain")
    clear.add_argument("hermes_home", type=Path)

    health = sub.add_parser("legacy-health")
    health.add_argument("hermes_home", type=Path)
    health.add_argument("unit")
    health.add_argument("argv_json")
    health.add_argument("source")
    health.add_argument("mode")
    health.add_argument("--max-age", type=int, default=30)

    args = parser.parse_args()
    try:
        if args.command == "legacy-drain":
            result = prove_legacy_drain(args.hermes_home, args.hold_json, args.repo, args.unit,
                                        args.pid, args.starttime, max_age=args.max_age,
                                        repeat_delay=args.repeat_delay,
                                        recovery_deadline=args.recovery_deadline,
                                        observe_timeout=args.observe_timeout)
        elif args.command == "clear-drain":
            result = clear_legacy_drain_request(args.hermes_home)
        else:
            result = prove_legacy_health(args.hermes_home, args.unit, _argv_json(args.argv_json),
                                         args.source, args.mode, max_age=args.max_age)
        print(json.dumps(result))
        return 0
    except Exception:
        if args.command == "legacy-drain":
            print("legacy bootstrap drain refused; hold/status evidence incomplete, stale or busy",
                  file=sys.stderr)
        elif args.command == "clear-drain":
            print("legacy bootstrap drain clear refused; marker missing, malformed or not owned",
                  file=sys.stderr)
        else:
            print("legacy bootstrap health refused; process/status evidence incomplete or stale",
                  file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
