#!/usr/bin/env python3
"""Read-only all-profile observation, freshly bound by guarded_switch.

An observation is not a reservation. No drain, locks, or live-state writes.
"""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import time


def rows(db, query):
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=3) as conn:
        return conn.execute(query).fetchall()


def observe(home, repo, pid, sha, query, proc=Path('/proc')):
    if (home / '.drain_request.json').exists():
        return False
    # Missing roots / unreadable stores are unknown, not an empty fleet.
    for folder in sorted((home / 'kanban/boards').iterdir()):
        db = folder / 'kanban.db'
        if not db.is_file() or not db.stat().st_size:
            continue
        if rows(db, "SELECT id FROM tasks WHERE status IN ('running','claimed') OR worker_pid IS NOT NULL"):
            return False
        if rows(db, 'SELECT id FROM task_runs WHERE ended_at IS NULL'):
            return False
    profiles = [home, *[p for p in sorted((home / 'profiles').iterdir()) if p.is_dir()]]
    for root in profiles:
        cron = root / 'cron'
        jobs = cron / 'jobs.json'
        if jobs.exists():
            payload = json.loads(jobs.read_text(encoding='utf-8'))
            payload = payload['jobs'] if isinstance(payload, dict) else payload
            if any(j.get('fire_claim') or j.get('run_claim') for j in payload):
                return False
        db = cron / 'executions.db'
        if db.exists() and rows(db, "SELECT job_id FROM executions WHERE lower(status) IN ('running','claimed','started','pending','in_progress')"):
            return False
        if list(cron.glob('.exec-*')):
            return False
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            cwd = os.readlink(process / 'cwd')
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if cwd == str(repo) or cwd.startswith(str(repo) + '/'):
            return False
    # Sample last so scanning the stores does not age the live activity proof.
    raw = query(home, 'status')
    if not isinstance(raw, dict):
        return False
    sample = raw.get('activity_sampled_at')
    return (raw.get('pid') == raw.get('answering_pid') == raw.get('activity_writer_pid') == pid
            and raw.get('code_sha') == sha and raw.get('gateway_state') == 'running'
            and raw.get('activity_state') == 'fresh'
            and type(raw.get('active_agents')) is int and raw['active_agents'] == 0
            and type(sample) in (int, float) and 0 <= time.time() - sample <= 5)


def main():
    try:
        home, repo, pid, sha = sys.argv[1:]
        home, repo = Path(home), Path(repo)
        spec = importlib.util.spec_from_file_location('idle_control', repo / 'gateway/control_socket.py')
        control = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(control)
        idle = observe(home, repo, int(pid), sha, control.query_gateway_control)
    except Exception:
        idle = False
    print(json.dumps({'idle_not_reserved': idle}))


if __name__ == '__main__':
    main()
