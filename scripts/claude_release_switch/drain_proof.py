#!/usr/bin/env python3
"""Strict admission-hold and gateway-drain evidence producer for release packets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys
import time

from controller import loads, private, require, shape
from runtime_health import live_observation


COVERAGE = {
    "gateway_turns",
    "cron",
    "api",
    "background_work",
    "kanban_workers",
    "updater",
    "editors",
    "source_readers",
    "recovery_handoff",
}


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
            require(response.get("ok") is True and type(response.get("result")) is dict, "gateway control socket refused")
            return response["result"]
        except Exception as exc:
            last_error = type(exc).__name__
            time.sleep(0.05)
    raise RuntimeError(last_error)


def _read_hold(path: Path, *, repo: str, unit: str, pid: int, starttime: str, max_age: int, recovery_deadline: int) -> None:
    hold = loads(private(path).read_bytes())
    shape(hold, {"kind": str, "repo": str, "unit": str, "pid": int, "starttime": str,
                 "observed": int, "valid_until": int, "recovery_deadline": int, "owner": str,
                 "recovery_owner": str, "approved": bool, "coverage": "environment"})
    require(hold["kind"] == "release-admission-hold" and hold["approved"] is True, "hold not approved")
    require(hold["repo"] == repo and hold["unit"] == unit, "hold target mismatch")
    require(hold["pid"] == pid and hold["starttime"] == starttime, "hold process mismatch")
    require(time.time() - max_age <= hold["observed"] <= time.time(), "hold stale")
    require(hold['owner'].strip() and hold['recovery_owner'].strip(), 'hold ownership missing')
    require(time.time() + max_age < recovery_deadline == hold['recovery_deadline'] <= hold['valid_until'], 'hold does not cover frozen recovery deadline')
    coverage = hold["coverage"]
    require(set(coverage) == COVERAGE, "hold coverage incomplete")
    for value in coverage.values():
        normalized = value.strip().lower()
        require(normalized and normalized not in {"unknown", "unverified", "missing", "none", "false"}, "unknown consumer hold")


def prove(home: Path, hold_path: Path, repo: str, unit: str, pid: int, starttime: str,
          *, max_age: int, repeat_delay: float, recovery_deadline: int) -> dict:
    _read_hold(hold_path, repo=repo, unit=unit, pid=pid, starttime=starttime, max_age=max_age, recovery_deadline=recovery_deadline)
    observations = []
    for _ in range(2):
        status = _query_socket(home, "status")
        observations.append(status)
        time.sleep(repeat_delay)
    for status in observations:
        require(status.get("pid") == pid and status.get("answering_pid") == pid, "gateway process mismatch")
        require(str(status.get("start_time")) == starttime, "gateway starttime mismatch")
        require(status.get("gateway_state") == "draining", "gateway not in drain state")
        observation = live_observation(status, max_age)
        require(observation.get('draining') is True, 'runner admission not draining')
    _read_hold(hold_path, repo=repo, unit=unit, pid=pid, starttime=starttime, max_age=max_age, recovery_deadline=recovery_deadline)
    return {"active_jobs": 0, "unit": unit, "pid": pid, "starttime": starttime, "observed": int(time.time())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hermes_home", type=Path)
    parser.add_argument("hold_json", type=Path)
    parser.add_argument("repo")
    parser.add_argument("unit")
    parser.add_argument("pid", type=int)
    parser.add_argument("starttime")
    parser.add_argument("--recovery-deadline", type=int, required=True)
    parser.add_argument("--max-age", type=int, default=30)
    parser.add_argument("--repeat-delay", type=float, default=0.2)
    args = parser.parse_args()
    try:
        print(json.dumps(prove(args.hermes_home, args.hold_json, args.repo, args.unit, args.pid,
                               args.starttime, max_age=args.max_age, repeat_delay=args.repeat_delay, recovery_deadline=args.recovery_deadline)))
        return 0
    except Exception:
        print("drain proof refused; hold/drain evidence incomplete, stale or busy", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
