#!/usr/bin/env python3
"""Deterministic one-shot bootstrap installation with frozen inverse recovery."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time

from controller import (BASE_ENV, PYTHON, SYSTEMCTL, Refusal, artifact_uid, cg_pids,
                        command_files, digest, durable, encoded, loads, private, proc,
                        raw, require, shape, show, system_env, systemd_seconds)


CMD = {'id': str, 'kind': str, 'argv': [str], 'cwd': str, 'env': 'environment', 'timeout': int}
SCHEMA = {
    'version': int, 'command_timeout': int, 'installation': str, 'state': str,
    'lock': str, 'lock_identity': {'device': int, 'inode': int},
    'unit': str, 'controller_unit': str,
    'baseline_unit_sha256': str, 'candidate_unit_sha256': str,
    'unit_definition_required': bool,
    'baseline': {'pid': int, 'starttime': str, 'cgroup': str, 'argv': [str]},
    'legacy': {'argv': [str], 'source': str},
    'bootstrap': {'argv': [str], 'source': str},
    'candidate': {'source': str, 'live': str, 'sha256': str, 'mode': int},
    'checks': {k: CMD for k in ('reload', 'drain', 'stop', 'clear-drain', 'start', 'health',
                                'recover-stop', 'recover-start', 'recover-health')},
    'window': {'start': int, 'abort': int, 'expiry': int, 'recovery_deadline': int,
               'reserve': int},
    'commands': [CMD], 'recovery': [CMD],
    'artifacts': [{'path': str, 'sha256': str, 'uid': int, 'mode': int}],
}

TERMINAL = ('bootstrap-active', 'legacy-recovered', 'preflight-blocked')


def cmd(identifier, kind, argv=None, cwd='/', env=None, timeout=10):
    return {'id': identifier, 'kind': kind, 'argv': argv or [], 'cwd': cwd,
            'env': env or {}, 'timeout': timeout}


def check(name, p):
    item = p['checks'][name]
    return cmd(name, 'command', item['argv'], item['cwd'], item['env'], item['timeout'])


def command_plan(p):
    forward = [
        cmd('install-candidate', 'internal', timeout=p['command_timeout']),
        check('reload', p),
        check('drain', p),
        check('stop', p),
        check('clear-drain', p),
        check('start', p),
        check('health', p),
    ]
    recovery = [
        check('recover-stop', p),
        cmd('remove-candidate', 'internal', timeout=p['command_timeout']),
        check('reload', p),
        check('clear-drain', p),
        check('recover-start', p),
        check('recover-health', p),
    ]
    return forward, recovery


def validate_command(command):
    require(command['kind'] in ('command', 'internal'), 'unknown command kind')
    require(1 <= command['timeout'] <= 300, 'command timeout')
    if command['kind'] == 'internal':
        require(command['argv'] == [] and command['cwd'] == '/' and command['env'] == {}, 'internal command shape')
        return
    command_files(command['argv'])
    require(Path(command['cwd']).is_absolute() and Path(command['cwd']).is_dir(), 'command cwd')
    allowed = set(BASE_ENV) | {'HOME', 'HERMES_HOME', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'}
    require(command['env'].keys() <= allowed and command['env'].get('PATH') == BASE_ENV['PATH'], 'unsafe command environment')


def cgroup_empty(cgroup):
    if not cgroup:
        return True
    return not cg_pids(cgroup)


def systemctl_show_props(unit, props):
    require(re.fullmatch(r'[a-zA-Z0-9_.@:\\-]+\.[a-zA-Z0-9]+', unit),
            'exact unit required')
    argv = [SYSTEMCTL, '--user', 'show', '--all']
    for prop in props:
        argv += ['-p', prop]
    argv += ['--', unit]
    p = subprocess.run(argv, cwd='/', env=system_env(), stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    values = dict(line.split('=', 1) for line in p.stdout.decode().splitlines() if '=' in line)
    values['_returncode'] = str(p.returncode)
    return values


class BootstrapRun:
    def __init__(self, packet, authority):
        self.path = Path(packet).absolute()
        require(not any(c in str(self.path) for c in '%$\n\r'), 'unsafe systemd packet path')
        self.state = private(self.path.parent, True)
        for entry in self.state.iterdir():
            private(entry)
        self.bytes = private(self.path).read_bytes()
        require(re.fullmatch('[a-f0-9]{64}', authority) and digest(self.bytes) == authority,
                'authority digest mismatch')
        self.authority = authority
        self.p = loads(self.bytes)
        shape(self.p, SCHEMA)
        p = self.p
        require(p['version'] == 1, 'unsupported bootstrap packet')
        require(Path(p['state']).resolve() == self.state, 'state path mismatch')
        require(1 <= p['command_timeout'] <= 300, 'command timeout')
        installation = private(p['installation'], True)
        require(installation.resolve() == installation, 'canonical installation required')
        for unit in (p['unit'], p['controller_unit']):
            require(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.@-]*\.service', unit), 'exact service required')
        require(p['unit'] != p['controller_unit'], 'unit collision')
        candidate_source = private(p['candidate']['source'])
        require(digest(candidate_source.read_bytes()) == p['candidate']['sha256'], 'candidate source drift')
        live = Path(p['candidate']['live'])
        require(live.is_absolute() and live.resolve() == live and not live.is_symlink(), 'candidate live path')
        require(live.name and not any(c in str(live) for c in '%$\n\r'), 'unsafe candidate live path')
        require(private(live.parent, True), 'candidate parent')
        require(p['candidate']['mode'] == 0o600, 'candidate mode')
        require(p['legacy']['argv'] and p['bootstrap']['argv'], 'runtime argv required')
        require(p['legacy']['source'].strip() and p['bootstrap']['source'].strip(), 'runtime source required')
        if p['unit_definition_required']:
            require(re.fullmatch('[a-f0-9]{64}', p['baseline_unit_sha256'])
                    and re.fullmatch('[a-f0-9]{64}', p['candidate_unit_sha256']),
                    'unit definition hash')
        w = p['window']
        require(w['start'] < w['abort'] < w['expiry'] <= w['recovery_deadline'], 'window order')
        require(w['reserve'] >= sum(c['timeout'] for c in p['recovery']) + 40, 'insufficient recovery reserve')
        require(w['expiry'] - w['abort'] >= stop_budget(w)
                and w['recovery_deadline'] - w['abort'] >= stop_budget(w), 'reserve not protected')
        for command in p['checks'].values():
            validate_command(command)
        require((p['commands'], p['recovery']) == command_plan(p), 'unapproved command/configuration')
        self.recovering = False
        self.lock_fd = None

    def log(self, event, **data):
        fd = os.open(self.state / 'journal.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'ab') as file:
            file.write(encoded({'time': time.time(), 'event': event, **data}))
            file.flush()
            os.fsync(file.fileno())

    def result(self, status, reason):
        durable(self.state / 'result.json', {'status': status, 'reason': reason,
                'authority': self.authority, 'time': time.time(),
                'operator_command': shlex.join([PYTHON, str(Path(__file__).resolve()), str(self.path),
                                                '--authority', self.authority, '--inspect-recovery']),
                'scope': 'bootstrap-installation',
                'data_restore': 'none; source/config inverse only, persistent data is not overwritten'})

    @contextlib.contextmanager
    def lock(self):
        path = Path(self.p['lock'])
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            private(path)
            st = os.fstat(fd)
            require({'device': st.st_dev, 'inode': st.st_ino} == self.p['lock_identity'], 'frozen lock inode changed')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_fd = fd
            self.lock_identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            self.log('lock-acquired', inode=self.lock_identity[1], recovery=self.recovering)
            yield
        finally:
            self.lock_fd = None
            os.close(fd)

    def locked(self):
        require(self.lock_fd is not None, 'lock not held')
        st = Path(self.p['lock']).stat()
        require((st.st_dev, st.st_ino) == self.lock_identity, 'lock inode changed')

    def marker(self, name):
        path = self.state / name
        return path.exists() and loads(private(path).read_bytes()) == {'authority': self.authority}

    def clock_gate(self, timeout=0):
        w = self.p['window']
        now = time.time()
        deadline = w['recovery_deadline'] if self.recovering else min(w['abort'], w['expiry'] - w['reserve'])
        require(math.isfinite(now) and (self.recovering or w['start'] <= now)
                and now + timeout + 2 < deadline, 'command deadline exhausted')

    def gate(self, timeout=0):
        self.clock_gate(timeout)
        require(private(self.path).read_bytes() == self.bytes, 'frozen packet changed')
        self.artifacts()
        self.clock_gate(timeout)

    def artifacts(self):
        expected = {str(Path(__file__).resolve()), str(Path(__file__).with_name('controller.py')),
                    PYTHON, SYSTEMCTL, '/usr/bin/systemd-run', self.p['candidate']['source']}
        for command in [*self.p['commands'], *self.p['recovery']]:
            if command['kind'] == 'command':
                expected.update(command_files(command['argv']))
        manifest = {a['path']: a for a in self.p['artifacts']}
        require(len(manifest) == len(self.p['artifacts']) and expected <= manifest.keys(), 'installation/executable manifest')
        for path, artifact in manifest.items():
            file = Path(path)
            require(file.is_absolute() and file.is_file(), 'artifact path')
            st = file.stat()
            require(artifact_uid(file) == artifact['uid'] and stat.S_IMODE(st.st_mode) == artifact['mode']
                    and not st.st_mode & 0o022, 'unsafe artifact permissions')
            require(digest(file.read_bytes()) == artifact['sha256'], 'installed artifact drift')

    def topology(self, recovery=False):
        s = show(self.p['controller_unit'])
        me = proc(os.getpid())
        require(me['cgroup'] == s['ControlGroup'] and s['ControlGroup'].endswith('/' + self.p['controller_unit']),
                'not independently supervised')
        require(not (self.p['baseline']['cgroup'] == me['cgroup']
                or me['cgroup'].startswith(self.p['baseline']['cgroup'] + '/')), 'gateway cgroup dependency')
        require(s['Restart'] == 'no' and s['KillMode'] == 'control-group'
                and s['KillSignal'] == '9' and s['FinalKillSignal'] == '9'
                and s['SendSIGKILL'] == 'yes' and s['TimeoutStopFailureMode'] == 'kill'
                and systemd_seconds(s['TimeoutStopUSec']) == self.p['window']['reserve']
                and not s.get('ExecStop'), 'unsafe supervisor lifecycle')
        for key in ('PartOf', 'BindsTo', 'PropagatesStopTo', 'StopPropagatedFrom'):
            require(not s.get(key), 'lifecycle coupling')
        require(set((s['Requires'] + ' ' + s['Wants']).split()) <= {'app.slice', 'basic.target'},
                'unapproved lifecycle dependencies')
        require(s['WorkingDirectory'] == str(self.state), 'supervisor cwd')
        require('--recover' in s['ExecStopPost'] and self.authority in s['ExecStopPost'],
                'external recovery guard missing')
        if recovery:
            require(cg_pids(me['cgroup']) == {os.getpid()}, 'takeover fencing not proven')
        else:
            require(s['MainPID'] == str(os.getpid()), 'not supervisor MainPID')
            durable(self.state / 'ready.json', {'cgroup': me['cgroup'], 'pid': os.getpid(),
                                                'authority': self.authority})
        self.log('fenced' if recovery else 'ready', cgroup=me['cgroup'], pid=os.getpid())

    def validate_unit_definition(self, expected_sha256, reason):
        if self.p['unit_definition_required']:
            actual = digest(raw([SYSTEMCTL, '--user', 'cat', self.p['unit']], '/', env=system_env()).encode())
            require(actual == expected_sha256, reason)

    def manager_reload_safe(self):
        command = [SYSTEMCTL, '--user', 'list-units', '--all', '--plain', '--no-legend', '--no-pager']
        p = subprocess.run(command, cwd='/', env=system_env(), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        require(p.returncode == 0, 'refusing daemon-reload; list-units failed')
        excluded = {self.p['unit'], self.p['controller_unit']}
        unit_names = []
        for line in p.stdout.decode().splitlines():
            fields = line.split()
            if fields:
                unit_names.append(fields[0])

        checked = 0
        pending = []
        for unit in sorted(set(unit_names)):
            if unit in excluded:
                continue
            snap = systemctl_show_props(unit, ['LoadState', 'NeedDaemonReload',
                                               'FragmentPath', 'DropInPaths'])
            if snap.get('_returncode') != '0':
                raise Refusal('refusing daemon-reload; show failed for loaded unit ' + unit)
            load_state = snap.get('LoadState')
            if not isinstance(load_state, str) or not load_state:
                raise Refusal('refusing daemon-reload; LoadState missing for loaded unit ' + unit)
            if load_state == 'loaded':
                need_reload = snap.get('NeedDaemonReload')
                if not isinstance(need_reload, str) or not need_reload:
                    raise Refusal('refusing daemon-reload; NeedDaemonReload missing for loaded unit ' + unit)
                checked += 1
                if need_reload != 'no':
                    pending.append(unit)
        require(not pending, 'refusing daemon-reload; unrelated loaded units need reload: ' + ', '.join(pending))
        self.log('reload-precheck', checked_loaded_units=checked)

    def baseline_process_active(self):
        status = show(self.p['unit'])
        require(status['ActiveState'] == 'active' and status['MainPID'] == str(self.p['baseline']['pid']),
                'baseline inactive')
        require(proc(self.p['baseline']['pid']) == self.p['baseline'], 'baseline identity changed')

    def baseline_active(self):
        self.baseline_process_active()
        self.validate_unit_definition(self.p['baseline_unit_sha256'], 'baseline service definition drift')
        require(not Path(self.p['candidate']['live']).exists(), 'candidate already installed')

    def candidate_unit_definition(self):
        self.validate_unit_definition(self.p['candidate_unit_sha256'], 'candidate service definition drift')

    def preflight(self):
        self.gate(sum(c['timeout'] for c in self.p['commands']))
        for marker in ('mutation-intent.json', 'stop-intent.json', 'consumed.json'):
            require(not (self.state / marker).exists(), 'stale intent marker')
        self.baseline_active()
        self.log('preflight-passed')

    def quiescent(self):
        status = show(self.p['unit'])
        group = status['ControlGroup'] or self.p['baseline']['cgroup']
        require(status['ActiveState'] in ('inactive', 'failed') and status['MainPID'] == '0',
                'service not proven inactive')
        require(cgroup_empty(group) and cgroup_empty(self.p['baseline']['cgroup']), 'service cgroup not empty')
        require(not Path('/proc', str(self.p['baseline']['pid'])).exists(), 'baseline process still exists')
        self.log('quiescent', unit=self.p['unit'])

    def install_candidate(self):
        live = Path(self.p['candidate']['live'])
        require(not live.exists(), 'candidate already installed')
        temp = live.with_name(live.name + '.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, self.p['candidate']['mode'])
        with os.fdopen(fd, 'wb') as file:
            shutil.copyfileobj(private(self.p['candidate']['source']).open('rb'), file)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp, self.p['candidate']['mode'])
        os.replace(temp, live)
        parent_fd = os.open(live.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        require(digest(private(live).read_bytes()) == self.p['candidate']['sha256'], 'candidate install drift')
        self.log('candidate-installed', path=str(live))

    def remove_candidate(self):
        live = Path(self.p['candidate']['live'])
        if not live.exists():
            require(self.marker('stop-intent.json'), 'candidate not installed')
            self.log('candidate-absent', path=str(live))
            return
        require(digest(private(live).read_bytes()) == self.p['candidate']['sha256'], 'candidate live bytes changed')
        live.unlink()
        parent_fd = os.open(live.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        self.log('candidate-removed', path=str(live))

    def validate_drain(self, output):
        proof = loads(output)
        shape(proof, {'active_jobs': int, 'unit': str, 'pid': int, 'starttime': str, 'observed': int})
        require(proof['active_jobs'] == 0 and proof['unit'] == self.p['unit']
                and proof['pid'] == self.p['baseline']['pid']
                and proof['starttime'] == self.p['baseline']['starttime']
                and self.p['window']['start'] <= proof['observed'] <= time.time(), 'no bound drain proof')
        durable(self.state / 'drain-proof.json', proof)

    def validate_health(self, output, recovered=False):
        proof = loads(output)
        shape(proof, {'healthy': bool, 'unit': str, 'pid': int, 'starttime': str,
                      'argv': [str], 'source': str, 'mode': str, 'observed': int})
        status = show(self.p['unit'])
        actual = proc(int(status['MainPID']))
        expected = self.p['legacy'] if recovered else self.p['bootstrap']
        require(status['ActiveState'] == 'active' and proof['healthy'] is True
                and proof['unit'] == self.p['unit'] and proof['pid'] == actual['pid']
                and proof['starttime'] == actual['starttime'] and proof['argv'] == expected['argv']
                and proof['source'] == expected['source']
                and proof['mode'] == ('legacy' if recovered else 'bootstrap')
                and self.p['window']['start'] <= proof['observed'] <= time.time(), 'runtime health mismatch')
        durable(self.state / ('legacy-health.json' if recovered else 'bootstrap-health.json'), proof)

    def command_started(self, op):
        return self.journal_has(lambda row: row.get('event') == 'intent' and row.get('op') == op)

    def journal_has(self, predicate):
        journal = self.state / 'journal.jsonl'
        if not journal.exists():
            return False
        for line in private(journal).read_text(encoding='utf-8').splitlines():
            try:
                row = loads(line.encode())
            except Exception:
                continue
            if predicate(row):
                return True
        return False

    def run_recovery_command_while_baseline_active(self, command):
        require(command['kind'] == 'command', 'recovery command shape')
        self.gate(command['timeout'])
        self.locked()
        self.baseline_process_active()
        self.log('intent', op=command['id'])
        if command['id'] == 'reload':
            self.manager_reload_safe()
        output = raw(command['argv'], command['cwd'], command['timeout'], command['env'])
        if command['id'] == 'recover-health':
            self.validate_health(output, recovered=True)
        self.log('done', op=command['id'])
        self.baseline_process_active()
        self.gate()
        return output

    def recover_before_stop(self):
        self.baseline_process_active()
        live = Path(self.p['candidate']['live'])
        if not live.exists():
            restore_started = self.journal_has(
                lambda row: row.get('event') == 'candidate-removed'
                or (row.get('event') == 'intent' and row.get('op') in ('reload', 'clear-drain', 'recover-health')))
            if not restore_started:
                self.result('preflight-blocked', 'bootstrap candidate was not installed; legacy left running')
                return
        elif digest(private(live).read_bytes()) != self.p['candidate']['sha256']:
            self.result('recovery-required', 'candidate live bytes changed before stop; legacy left running')
            return
        else:
            self.log('recovery-begin', model_used=False, pre_stop=True)
            self.remove_candidate()

        self.baseline_process_active()
        self.run_recovery_command_while_baseline_active(check('reload', self.p))
        self.validate_unit_definition(self.p['baseline_unit_sha256'],
                                      'baseline service definition not restored')
        if self.command_started('drain') or (self.state / 'drain-proof.json').exists():
            self.run_recovery_command_while_baseline_active(check('clear-drain', self.p))
        self.run_recovery_command_while_baseline_active(check('recover-health', self.p))
        self.result('preflight-blocked',
                    'bootstrap candidate removed before stop; legacy left running')

    def execute(self, command):
        self.gate(command['timeout'])
        self.locked()
        self.log('intent', op=command['id'])
        if command['id'] == 'install-candidate':
            durable(self.state / 'mutation-intent.json', {'authority': self.authority}, exclusive=True)
            self.result('recovery-required', 'bootstrap configuration mutation may have begun')
            self.install_candidate()
        elif command['id'] == 'stop':
            durable(self.state / 'stop-intent.json', {'authority': self.authority}, exclusive=True)
            output = raw(command['argv'], command['cwd'], command['timeout'], command['env'])
        elif command['id'] == 'remove-candidate':
            self.quiescent()
            self.remove_candidate()
        else:
            if command['id'] == 'reload':
                self.manager_reload_safe()
            output = raw(command['argv'], command['cwd'], command['timeout'], command['env'])
            if command['id'] == 'drain':
                self.validate_drain(output)
            elif command['id'] == 'health':
                self.validate_health(output)
            elif command['id'] == 'recover-health':
                self.validate_health(output, recovered=True)
        self.log('done', op=command['id'])
        self.gate()

    def forward(self):
        with self.lock():
            require(not (self.state / 'consumed.json').exists(), 'single-use packet already consumed')
            self.preflight()
            self.topology()
            durable(self.state / 'consumed.json', {'authority': self.authority}, exclusive=True)
            for command in self.p['commands']:
                self.execute(command)
                if command['id'] == 'reload':
                    self.baseline_process_active()
                    self.candidate_unit_definition()
                if command['id'] == 'drain':
                    self.baseline_process_active()
                    self.candidate_unit_definition()
                if command['id'] == 'stop':
                    self.quiescent()
                if command['id'] == 'health':
                    self.candidate_unit_definition()
            self.result('bootstrap-active', 'same service restarted with approved bootstrap configuration')

    def recover(self):
        self.recovering = True
        if (self.state / 'result.json').exists():
            status = loads(private(self.state / 'result.json').read_bytes())['status']
            if status in TERMINAL:
                return
        if not (self.state / 'mutation-intent.json').exists():
            self.result('preflight-blocked', 'supervisor ended before bootstrap mutation intent')
            return
        require(loads(private(self.state / 'mutation-intent.json').read_bytes()) == {'authority': self.authority},
                'mutation intent belongs to another packet')
        self.result('recovery-required', 'recovery not yet proven; keep held on uncertainty')
        with self.lock():
            self.gate(sum(c['timeout'] for c in self.p['recovery']))
            self.topology(recovery=True)
            stop_may_have_begun = self.marker('stop-intent.json')
            live = Path(self.p['candidate']['live'])
            if not stop_may_have_begun:
                self.recover_before_stop()
                return
            self.log('recovery-begin', model_used=False)
            for command in self.p['recovery']:
                if command['id'] == 'remove-candidate':
                    self.quiescent()
                self.execute(command)
                if command['id'] == 'recover-stop':
                    self.quiescent()
                if command['id'] == 'reload' and self.p['unit_definition_required']:
                    self.validate_unit_definition(self.p['baseline_unit_sha256'],
                                                  'baseline service definition not restored')
            self.result('legacy-recovered', 'known-good legacy target restarted; persistent data was not restored or overwritten')


def stop_budget(window):
    return 2 * window['reserve'] + window['reserve'] + 10


def inspect_recovery(packet):
    state = private(Path(packet).absolute().parent, True)
    evidence = loads(private(state / 'result.json').read_bytes())
    units = {}
    try:
        packet_data = loads(private(packet).read_bytes())
        for key in ('unit', 'controller_unit'):
            unit = packet_data[key]
            require(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.@-]*\.service', unit), 'diagnostic unit')
            units[key] = show(unit)
    except Exception:
        units = {'unavailable': 'use retained approved unit identity'}
    print(json.dumps({'result': evidence, 'untrusted_packet_readback': units,
                      'next_action': 'Keep the hold. Remove only exact candidate bytes, start known-good legacy, and preserve persistent data.'}))


def launch(run):
    with run.lock():
        run.preflight()
        require(not (run.state / 'consumed.json').exists(), 'packet already consumed')
    argv = [PYTHON, str(Path(__file__).resolve()), str(run.path), '--authority', run.authority]
    post = shlex.join(argv + ['--recover'])
    timeout = max(1, run.p['window']['recovery_deadline'] - int(time.time()))
    props = {'Type': 'exec', 'Restart': 'no', 'UMask': '0077', 'KillMode': 'control-group',
             'TimeoutStopSec': str(run.p['window']['reserve']), 'SendSIGKILL': 'yes',
             'KillSignal': 'SIGKILL', 'FinalKillSignal': 'SIGKILL', 'TimeoutStopFailureMode': 'kill',
             'WorkingDirectory': str(run.state), 'ExecStopPost': post,
             'RuntimeMaxSec': str(max(1, run.p['window']['abort'] - int(time.time()))),
             'StandardOutput': 'null', 'StandardError': 'null', 'NoNewPrivileges': 'yes',
             'ProtectControlGroups': 'yes'}
    command = ['/usr/bin/systemd-run', '--user', '--unit=' + run.p['controller_unit']]
    command += ['--property=' + k + '=' + v for k, v in props.items()]
    raw(command + argv + ['--supervise'], run.state, min(10, timeout), system_env())
    print(json.dumps({'unit': run.p['controller_unit'], 'result': str(run.state / 'result.json')}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('packet', type=Path)
    parser.add_argument('--authority', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--supervise', action='store_true', help=argparse.SUPPRESS)
    mode.add_argument('--recover', action='store_true', help=argparse.SUPPRESS)
    mode.add_argument('--inspect-recovery', action='store_true')
    args = parser.parse_args()
    run = None
    try:
        if args.inspect_recovery:
            inspect_recovery(args.packet)
            return 0
        run = BootstrapRun(args.packet, args.authority)
        if args.recover:
            run.recover()
        elif args.supervise:
            run.forward()
        elif args.apply:
            launch(run)
        else:
            with run.lock():
                run.preflight()
            print('preflight-passed (no lifecycle or bootstrap mutation performed)')
        return 0
    except Exception as exc:
        reason = str(exc) if isinstance(exc, Refusal) else type(exc).__name__
        if run is None:
            try:
                state = private(args.packet.absolute().parent, True)
                status = 'recovery-required' if (state / 'mutation-intent.json').exists() else 'preflight-blocked'
                durable(state / 'result.json', {'status': status, 'reason': reason,
                                                'scope': 'bootstrap-installation'})
            except Exception:
                pass
        else:
            run.log('blocked', reason=reason)
            mutated = (run.state / 'mutation-intent.json').exists()
            if args.recover or not mutated:
                run.result('recovery-required' if mutated else 'preflight-blocked', reason)
        print('blocked: ' + reason, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
