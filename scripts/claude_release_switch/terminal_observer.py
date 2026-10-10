#!/usr/bin/env python3
"""Disposable terminal-owner check. See guarded_switch.md for finite cron wiring.

No release actions, model calls, or direct delivery. Stdout is a delivery request,
never acknowledgment. The owner alone records a receipt with --ack/--receipt.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time


def require(ok):
    if not ok:
        raise ValueError('invalid observer binding/state')


def read(p):
    require(p.resolve(strict=True) == p and not p.is_symlink())
    s = p.stat()
    require(stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid()  # windows-footgun: ok — Linux/systemd-only observer
            and stat.S_IMODE(s.st_mode) == 0o600 and s.st_nlink == 1)
    return p.read_bytes()


def save(directory, state):
    target = directory / 'observer.json'
    if target.exists() or target.is_symlink():
        read(target)
    fd, name = tempfile.mkstemp(dir=directory, prefix='.observer-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, target)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def show(unit):
    p = subprocess.run(['/usr/bin/systemctl', '--user', 'show', unit],
                       env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                            'XDG_RUNTIME_DIR': f'/run/user/{os.getuid()}'},  # windows-footgun: ok — Linux/systemd-only observer
                       capture_output=True, text=True, timeout=5, check=True)
    return dict(line.split('=', 1) for line in p.stdout.splitlines() if '=' in line)


def load(filename, sha):
    raw = read(Path(filename))
    require(hashlib.sha256(raw).hexdigest() == sha)
    b = json.loads(raw)
    require(set(b) == {'executor', 'invocation', 'manifest_sha256', 'journal',
                       'owner', 'state_dir', 'expires_at', 'max_attempts', 'retry_seconds'})
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*\.service', b['executor']))
    require(re.fullmatch(r'[0-9a-f]{32}', b['invocation']))
    require(re.fullmatch(r'[0-9a-f]{64}', b['manifest_sha256']))
    require(isinstance(b['owner'], str) and 0 < len(b['owner']) <= 200)
    require(type(b['max_attempts']) is int and 2 <= b['max_attempts'] <= 5)
    require(type(b['retry_seconds']) is int and 30 <= b['retry_seconds'] <= 300)
    require(type(b['expires_at']) in (float, int) and 0 < b['expires_at'] < 10**11)
    directory = Path(b['state_dir'])
    require(directory.resolve(strict=True) == directory and directory.is_dir())
    require(directory.stat().st_uid == os.getuid() and stat.S_IMODE(directory.stat().st_mode) == 0o700)  # windows-footgun: ok — Linux/systemd-only observer
    require(Path(b['journal']).is_absolute())
    return b, directory


def observe(b, now, inspect=show):
    try:
        s = inspect(b['executor'])
        require(s.get('Id') == b['executor'] and s.get('InvocationID') == b['invocation']
                and s.get('LoadState') == 'loaded')
        # ExecStopPost is ControlPID; MainPID=0 alone is NOT terminal.
        if (int(s['MainPID']) != 0 or int(s['ControlPID']) != 0
                or s['ActiveState'] in ('active', 'activating', 'deactivating')):
            return None
        require(s['ActiveState'] in ('inactive', 'failed'))
        if now >= b['expires_at']:
            return 'expired_unacknowledged'
        journal = Path(b['journal'])
        if not journal.exists():
            return 'terminal_not_armed'
        j = json.loads(read(journal))
        require(j['manifest'] == b['manifest_sha256'] and j['executor_invocation'] == b['invocation'])
        if j.get('phase') == 'complete' and j.get('outcome') in ('switched', 'recovered'):
            return 'terminal_' + j['outcome']
        return 'terminal_recovery_required'
    except Exception:
        return 'expired_unknown' if now >= b['expires_at'] else 'observation_failed'


def run(filename, sha, *, ack=None, receipt=None, now=None, inspect=show):
    b, directory = load(filename, sha)
    lock = directory / 'lock'
    read(lock)  # Provision externally; no unexpected state directory creation.
    with lock.open('r+') as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        target = directory / 'observer.json'
        state = json.loads(read(target)) if target.exists() else {'binding': sha, 'attempts': 0}
        require(state['binding'] == sha)
        now = time.time() if now is None else now
        if ack is not None:
            require(ack == state.get('token') and isinstance(receipt, str) and bool(receipt.strip()))
            state.update(status='acknowledged', receipt=receipt)
            save(directory, state)
            return ''
        if state.get('status') == 'acknowledged':
            return ''
        kind = observe(b, now, inspect)
        # Expiry is an owned blocker even when output is forbidden or all
        # emission attempts are spent. Never manufacture a receipt/terminality.
        if now >= b['expires_at']:
            state['expired'] = True
            state.setdefault('expiry_observed_at', now)
            state['expiry_blocker'] = (
                'owner_live' if kind is None else
                'terminality_unverified' if kind == 'expired_unknown' else
                'unacknowledged_terminal')
            state.setdefault('status', 'expired_unacknowledged')
            save(directory, state)
        if kind is None:
            # An interrupted silent invocation consumes no script send budget.
            return ''
        if state['attempts'] >= b['max_attempts']:
            return ''
        if now < state.get('next_attempt_at', 0):
            return ''
        # Freeze the first owned notice; a failure is not a release verdict.
        if 'token' not in state:
            state.update(kind=kind, token=hashlib.sha256((sha + ':' + kind).encode()).hexdigest())
        state['attempts'] += 1
        state.update(next_attempt_at=now + b['retry_seconds'],
                     status='unacknowledged_exhausted' if state['attempts'] == b['max_attempts']
                     else 'pending_ack', expired=now >= b['expires_at'])
        # Persist BEFORE stdout. Crash here may lose this attempt; later ticks
        # retain the remaining budget. A send/crash can duplicate the same token.
        save(directory, state)
        return json.dumps({'owner': b['owner'], 'observer': state['kind'], 'token': state['token'],
                           'executor': b['executor'], 'invocation': b['invocation'],
                           'status': state['status'], 'expired': state['expired'],
                           'request': 'Acknowledge receipt; inspect journal. No release retry or TEST mutation. '
                           'No takeover until fresh exact executor AND recovery hook terminality '
                           'is verified; failure acknowledgment is not that verification.'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binding')
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--ack')
    parser.add_argument('--receipt')
    args = parser.parse_args()
    try:
        output = run(args.binding, args.sha256, ack=args.ack, receipt=args.receipt)
        if output:
            print(output)
        return 0
    except Exception:
        # No journal/config contents or subprocess stderr in scheduler alerts.
        print('Terminal observer failed; owner must inspect its binding/state.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
