#!/usr/bin/env python3
"""Collect strict live runtime health and feed the release health contract."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
import time

from controller import durable, loads, private, require
from health import read_health


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


def _read_startup(startup_json: Path, pid: int, starttime: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    last_error = "startup evidence missing"
    while time.monotonic() < deadline:
        try:
            started = loads(private(startup_json).read_bytes())
            if started.get("pid") == pid and str(started.get("starttime")) == starttime:
                return started
            last_error = "startup evidence stale"
        except Exception as exc:
            last_error = type(exc).__name__
        time.sleep(0.05)
    raise RuntimeError(last_error)


def _check_state_db(home: Path) -> None:
    db = home / "state.db"
    require(db.exists(), "state.db missing")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    try:
        for table in ("sessions", "messages"):
            row = conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            require(row is None or row == (1,), table + " table unreadable")
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        require(mode and str(mode[0]).lower() in {"wal", "delete", "truncate", "persist", "memory", "off"}, "journal mode unreadable")
    finally:
        conn.close()


def _check_cron(home: Path, max_age: int, not_before: float) -> None:
    cron = home / "cron"
    for name in ("ticker_heartbeat", "ticker_last_success"):
        try:
            stamp = float((cron / name).read_text(encoding="utf-8").strip())
            age = time.time() - stamp
        except Exception:
            raise RuntimeError("cron " + name + " missing")
        require(0 <= age <= max_age, "cron " + name + " stale")
        require(stamp >= not_before, "cron " + name + " predates current gateway")


def _profile_homes(root_home: Path, identify: dict, status: dict) -> dict[str, Path]:
    served = identify.get("served_profiles") or status.get("served_profiles") or []
    if not served:
        served = [identify.get("profile") or "default"]
    require(type(served) is list and served, "served profiles missing")
    explicit = status.get("served_profile_homes") or identify.get("served_profile_homes") or {}
    require(type(explicit) is dict, "served profile homes malformed")
    homes: dict[str, Path] = {}
    for item in served:
        require(type(item) is str and re.fullmatch(r"default|[a-z0-9][a-z0-9_-]{0,63}", item), "served profile malformed")
        if item in explicit:
            candidate = Path(str(explicit[item])).resolve(strict=True)
        elif item == "default":
            candidate = root_home
        elif root_home.parent.name == "profiles":
            candidate = (root_home.parent / item).resolve(strict=True)
        else:
            candidate = (root_home / "profiles" / item).resolve(strict=True)
        require(candidate.is_dir(), "served profile home missing")
        homes[item] = candidate
    require(len(homes) == len(served), "duplicate served profile")
    return homes


def live_observation(status: dict, max_age: int) -> dict:
    observation = status.get('release_observation')
    require(type(observation) is dict, 'live runner observation missing')
    stamp = observation.get('observed')
    require(type(stamp) in (int, float) and 0 <= time.time() - stamp <= max_age, 'runner observation stale')
    started = observation.get('started')
    require(type(started) in (int, float) and 0 < started <= stamp, 'runner generation missing')
    work = observation.get('work')
    require(type(work) is dict and set(work) == {'turns', 'cron', 'api', 'background'}, 'work observation missing')
    for key in ('turns', 'cron', 'api'):
        require(type(work[key]) is int and work[key] == 0, 'gateway busy: ' + key)
    require(work['background'] is False, 'gateway background work busy or unknown')
    return observation


def collect(startup_json: Path, live_json: Path, unit: str, home: Path, max_age: int) -> dict:
    home = home.resolve(strict=True)
    identify = _query_socket(home, "identify")
    status = _query_socket(home, "status")
    pid = int(identify["pid"])
    starttime = str(identify["start_time"])
    started = _read_startup(startup_json, pid, starttime)
    require(identify.get("pid") == pid and str(identify.get("start_time")) == starttime, "identify process mismatch")
    require(status.get("answering_pid") == pid and status.get("pid") == pid, "status process mismatch")
    require(str(status.get("start_time")) == starttime, "status starttime mismatch")
    require(status.get("gateway_state") == "running", "gateway not running")
    observation = live_observation(status, max_age)
    require(observation.get('running') is True and observation.get('draining') is False, 'runner not ready')
    profiles = observation.get('profiles')
    require(type(profiles) is dict and profiles, 'live profiles missing')
    # Only exact homes captured from the actual runner resolver are accepted.
    status = {**status, 'served_profile_homes': {name: value['home'] for name, value in profiles.items()}}
    homes = _profile_homes(home, identify, status)
    require(set(homes) == set(profiles), 'served profile observation mismatch')
    for profile, profile_home in homes.items():
        state = profiles[profile]
        require(state.get('sessions') is True, 'profile session store unavailable')
        platforms = state.get('platforms')
        require(type(platforms) is dict and platforms and all(value is True for value in platforms.values()), 'profile transport unavailable')
        _check_cron(profile_home, max_age, observation['started'])
        _check_state_db(profile_home)
    live = {
        "pid": pid,
        "starttime": starttime,
        "observed": int(time.time()),
        "platform": "ok",
        "scheduler": "ok",
        "persistence": "ok",
        "sessions": "ok",
        "served_profile_homes": {profile: str(profile_home) for profile, profile_home in sorted(homes.items())},
    }
    durable(live_json, live)
    return read_health(startup_json, live_json, unit)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("startup_json", type=Path)
    parser.add_argument("live_json", type=Path)
    parser.add_argument("unit")
    parser.add_argument("hermes_home", type=Path)
    parser.add_argument("--max-age", type=int, default=90)
    parser.add_argument("--startup-timeout", type=int, default=90,
                        help="seconds to retry incomplete readiness (0..240; default 90)")
    args = parser.parse_args()
    if not 0 <= args.startup_timeout <= 240:
        parser.error("--startup-timeout must be between 0 and 240")
    deadline = time.monotonic() + args.startup_timeout
    while True:
        try:
            result = collect(args.startup_json, args.live_json, args.unit, args.hermes_home, args.max_age)
            print(json.dumps(result))
            return 0
        except Exception:
            if time.monotonic() >= deadline:
                print("runtime health refused; evidence incomplete, stale or not live", file=sys.stderr)
                return 2
            time.sleep(min(.25, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    raise SystemExit(main())
