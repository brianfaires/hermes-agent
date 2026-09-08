#!/usr/bin/env python3
"""Finite release executor with externally frozen authority and independent recovery.

The inline windows-footgun suppressions in this Linux-only controller are
limited to deliberate POSIX ownership, mode, UID, process, and user-systemd
checks that enforce the release safety boundary.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import pwd
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time

GIT = '/usr/bin/git'
SYSTEMCTL = '/usr/bin/systemctl'
PYTHON = '/usr/bin/python3'
CLAUDE = '/home/brian/.local/bin/claude'
BASE_ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
            'GIT_OPTIONAL_LOCKS': '0'}
RECEIPTS = ('authority', 'ci', 'review', 'backup', 'compatibility', 'drain',
            'sole_writer', 'git_credentials', 'readiness')
TERMINAL = ('succeeded', 'rolled-back', 'preflight-blocked')


class Refusal(Exception):
    pass


def require(ok, message):
    if not ok:
        raise Refusal(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def private(path, directory=False):
    path = Path(path)
    s = path.lstat()
    require(not path.is_symlink() and s.st_uid == os.getuid(), 'unsafe owner/symlink')  # windows-footgun: ok
    require(stat.S_IMODE(s.st_mode) == (0o700 if directory else 0o600), 'unsafe permissions')
    require(stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode), 'unsafe file type')
    return path


def artifact_uid(path):
    # User-systemd filesystem protection may map host root to the overflow UID.
    # Freeze root's logical identity, not that namespace-local numeric mapping.
    uid = Path(path).stat().st_uid
    return 0 if uid == Path('/').stat().st_uid else uid


def durable(path, value, exclusive=False):
    path = Path(path)
    data = encoded(value)
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    else:
        temp = path.with_name(path.name + '.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def loads(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate JSON key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(Refusal('nonfinite JSON')))


def shape(value, template, where='packet'):
    """Exact schema, including bool != int; list templates specify element type."""
    if isinstance(template, dict):
        require(type(value) is dict and value.keys() == template.keys(), f'{where}: keys')
        for key in template:
            shape(value[key], template[key], where + '.' + key)
    elif isinstance(template, list):
        require(type(value) is list, f'{where}: list')
        for item in value:
            shape(item, template[0], where)
    elif template == 'environment':
        require(type(value) is dict and all(type(k) is str and type(v) is str for k, v in value.items()), f'{where}: environment')
    else:
        require(type(value) is template, f'{where}: type')


REV = {'sha': str, 'tree': str, 'branch': str, 'files': [
    {'path': str, 'sha256': str, 'mode': str}]}
LOADED_SOURCE = [{'module': str, 'path': str, 'sha256': str}]
COMMAND = {'id': str, 'argv': [str], 'cwd': str, 'env': BASE_ENV.copy(), 'timeout': int}
COMMAND['env'] = 'environment'
SCHEMA = {
    'version': int, 'command_timeout': int, 'installation': str, 'lock': str, 'profile': str, 'repo': str, 'remote': str,
    'git_route': {'home': str, 'helper': str, 'ssh': str},
    'git_config': [str], 'extra_refs': [{'ref': str, 'sha': str}],
    'untracked': [{'path': str, 'sha256': str, 'mode': int, 'classification': str, 'reason': str}],
    'checks': {k: COMMAND for k in ('drain', 'offline', 'smoke', 'recover-offline', 'recover-smoke')},
    'drain_path': str, 'health_path': str,
    'repo_identity': {'device': int, 'inode': int},
    'lock_identity': {'device': int, 'inode': int},
    'order': str, 'runbook': {'path': str, 'sha256': str},
    'operation': str, 'current': REV, 'candidate': REV, 'rollback': REV,
    'local': {'main': str, 'staging': str, 'rollback': str},
    'authoritative': {'main': str, 'staging': str},
    'unit': str, 'unit_definition_sha256': str, 'controller_unit': str, 'launcher': str, 'launcher_sha256': str,
    'baseline': {'pid': int, 'starttime': str, 'cgroup': str, 'argv': [str]},
    'window': {'start': int, 'abort': int, 'expiry': int, 'recovery_deadline': int,
               'reserve': int},
    'receipts': {key: {'path': str, 'sha256': str} for key in RECEIPTS},
    'commands': [COMMAND], 'recovery': [COMMAND],
    'model': {'kind': str, 'name': str, 'timeout': int},
    'artifacts': [{'path': str, 'sha256': str, 'uid': int, 'mode': int}],
}


def raw(argv, cwd, timeout=10, env=None):
    """Never log raw stdout/stderr or argv supplied by untrusted processes."""
    p = subprocess.run(argv, cwd=cwd, env=env or BASE_ENV, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    require(p.returncode == 0, f'command failed ({p.returncode}); output redacted')
    require(len(p.stdout) < 4 * 1024 * 1024, 'oversized output')
    return p.stdout.decode().strip()


def git(repo, *args):
    return raw([GIT, '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'submodule.recurse=false', *args], repo)


def proc(pid):
    root = Path('/proc') / str(pid)
    text = (root / 'stat').read_text(encoding='utf-8')
    return {'pid': pid, 'starttime': text[text.rindex(')') + 2:].split()[19],
            'cgroup': (root / 'cgroup').read_text(encoding='utf-8').strip().removeprefix('0::'),
            'argv': (root / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')}


def show(unit):
    text = raw([SYSTEMCTL, '--user', 'show', unit, '--all',
                '-p', 'MainPID', '-p', 'ActiveState', '-p', 'SubState', '-p', 'ControlGroup',
                '-p', 'PartOf', '-p', 'BindsTo', '-p', 'Requires', '-p', 'Wants',
                '-p', 'PropagatesStopTo', '-p', 'StopPropagatedFrom', '-p', 'Restart',
                '-p', 'KillMode', '-p', 'KillSignal', '-p', 'FinalKillSignal',
                '-p', 'SendSIGKILL', '-p', 'TimeoutStopUSec', '-p', 'TimeoutStopFailureMode',
                '-p', 'ExecStop', '-p', 'ExecStopPost', '-p', 'WorkingDirectory',
                '-p', 'NeedDaemonReload', '-p', 'FragmentPath', '-p', 'DropInPaths'], '/', env=system_env())
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def system_env():
    uid = os.getuid()  # windows-footgun: ok
    return {**BASE_ENV, 'HOME': pwd.getpwuid(os.getuid()).pw_dir, 'XDG_RUNTIME_DIR': f'/run/user/{uid}',  # windows-footgun: ok
            'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{uid}/bus'}


def cg_pids(cgroup):
    require(cgroup.startswith('/user.slice/') and '..' not in cgroup, 'invalid cgroup')
    root = Path('/sys/fs/cgroup') / cgroup.lstrip('/')
    if not root.exists():
        return set()
    return {int(x) for file in root.rglob('cgroup.procs') for x in file.read_text(encoding='utf-8').split()}


def revision(repo, sha, branch):
    listing = raw([GIT, 'ls-tree', '-rz', sha], repo)
    entries = [entry.split('\t', 1) for entry in listing.rstrip('\x00').split('\x00')] if listing else []
    blobs = [meta.split()[2] for meta, _ in entries]
    batch = subprocess.run([GIT, 'cat-file', '--batch'], input=('\n'.join(blobs) + '\n').encode(),
                           cwd=repo, env=BASE_ENV, capture_output=True, timeout=30)
    require(batch.returncode == 0, 'blob inventory failed')
    data, offset, files = batch.stdout, 0, []
    for meta, path in entries:
        mode, kind, blob = meta.split()
        require(mode in ('100644', '100755') and kind == 'blob', 'only regular source supported')
        end = data.index(b'\n', offset)
        header = data[offset:end].decode().split()
        require(header[:2] == [blob, 'blob'], 'blob identity')
        size = int(header[2])
        content = data[end + 1:end + 1 + size]
        require(len(content) == size, 'truncated blob')
        offset = end + size + 2
        files.append({'path': path, 'sha256': digest(content), 'mode': mode})
    return {'sha': sha, 'tree': git(repo, 'rev-parse', sha + '^{tree}'), 'branch': branch, 'files': files}


def executable_access(path):
    path = Path(path)
    require(path.is_absolute() and path.is_file(), 'absolute executable required')
    require(os.access(path, os.X_OK, effective_ids=True), 'executable access denied')


def command_files(argv):
    require(argv and all(type(x) is str and '\x00' not in x for x in argv), 'invalid argv')
    executable = Path(argv[0])
    executable_access(executable)
    name = executable.resolve().name
    require(name not in ('sh', 'bash', 'dash', 'zsh', 'fish', 'env', 'sudo', 'su', 'perl', 'ruby', 'node', 'nodejs'),
            'shell/interpreter escape')
    files = {str(executable)}
    if name.startswith('python'):
        require(len(argv) >= 2, 'pinned Python script required')
        if argv[1:6] == ['-I', '-S', '-B', '-X', 'pycache_prefix=/dev/null']:
            argv = [argv[0], *argv[6:]]
        elif argv[1:3] == ['-I', '-S']:
            argv = [argv[0], *argv[3:]]
        require(argv[1] not in ('-c', '--command', '-m', '-e', '--eval', 'eval', 'exec'),
                'shell/interpreter escape')
        require(Path(argv[1]).is_absolute() and Path(argv[1]).is_file(), 'pinned Python script required')
        files.add(argv[1])
    else:
        first = executable.read_bytes().split(b'\n', 1)[0]
        if first.startswith(b'#!'):
            interpreter = first[2:].decode().split()
            require(len(interpreter) == 1 and Path(interpreter[0]).is_absolute(), 'direct pinned shebang required')
            require(Path(interpreter[0]).resolve().name.startswith('python'), 'unsupported script interpreter')
            executable_access(interpreter[0])
            files.add(interpreter[0])
    return files


def baseline_files(argv):
    """Observed service identity only; never authorizes executing check argv."""
    require(argv and all(type(x) is str and '\x00' not in x for x in argv), 'invalid argv')
    executable = Path(argv[0])
    executable_access(executable)
    name = executable.resolve().name
    if len(argv) == 5 and argv[1:] == ['-m', 'hermes_cli.main', 'gateway', 'run']:
        require(name.startswith('python'), 'module launcher requires Python executable')
        return {str(executable)}
    return command_files(argv)


def validate_check(command, p):
    command_files(command['argv'])
    require(Path(command['cwd']).is_absolute() and Path(command['cwd']).is_dir(), 'check cwd')
    require(1 <= command['timeout'] <= 300, 'check timeout')
    allowed = set(BASE_ENV) | {'HOME', 'HERMES_HOME', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'}
    require(command['env'].keys() <= allowed and command['env'].get('PATH') == BASE_ENV['PATH'], 'unsafe check environment')
    # A frozen standalone check is trusted operator code. Its complete identity is
    # pinned; the model can neither select its arguments nor supply an interpreter.
    require(command['argv'][0] not in (GIT, SYSTEMCTL, '/usr/bin/systemd-run'), 'check cannot replace exact lifecycle/git operations')


def validate_route(p):
    route = p['git_route']
    private(route['home'], True)
    require(Path(route['home']).is_absolute() and route['home'] != claude_env()['HOME'], 'isolated Git HOME required')
    remote = p['remote']
    require(not remote.startswith('-') and not any(c.isspace() for c in remote), 'unsafe remote')
    if remote.startswith('/'):
        require(Path(remote).resolve() == Path(remote) and not route['helper'] and not route['ssh'], 'local Git route')
        require(git(remote, 'rev-parse', '--is-bare-repository') == 'true', 'local bare remote required')
    elif remote.startswith('https://'):
        from urllib.parse import urlsplit
        url = urlsplit(remote)
        require(url.hostname and not url.username and not url.password and not url.query and not url.fragment,
                'credential-free HTTPS URL required')
        require(route['helper'] and not route['ssh'], 'explicit HTTPS credential helper required')
    else:
        require(re.fullmatch(r'[a-zA-Z0-9_.-]+@[a-zA-Z0-9.-]+:[a-zA-Z0-9_./-]+', remote)
                and route['ssh'] and not route['helper'], 'explicit SSH route required')
    for key in ('helper', 'ssh'):
        if route[key]:
            require(re.fullmatch(r'/[a-zA-Z0-9_./-]+', route[key]), 'credential executable path')
            command_files([route[key]])


def git_env(p):
    env = {**BASE_ENV, 'HOME': p['git_route']['home']}
    if p['git_route']['ssh']:
        env.update(GIT_SSH=p['git_route']['ssh'], GIT_SSH_VARIANT='ssh')
    return env


def git_argv(p, *args):
    argv = [GIT, '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'submodule.recurse=false', '-c', 'credential.helper=']
    if p['git_route']['helper']:
        argv += ['-c', 'credential.helper=' + p['git_route']['helper']]
    return argv + list(args)


def untracked(repo, approved):
    """Inventory every ignored/untracked file, including symlinks, without reading secrets."""
    expected = {x['path']: x for x in approved}
    require(len(expected) == len(approved), 'duplicate untracked path')
    output = subprocess.check_output([GIT, 'ls-files', '--others', '-z'], cwd=repo, env=BASE_ENV)
    result = []
    for name in sorted(output.decode().rstrip('\x00').split('\x00')) if output else []:
        require(name in expected, 'unclassified untracked/ignored file')
        item = expected[name]
        require(item['classification'] in ('backed-up', 'reproducible') and item['reason'].strip(), 'untracked classification')
        path = Path(repo) / name
        require(path.parent.resolve().is_relative_to(Path(repo)), 'untracked parent escapes')
        st = path.lstat()
        require(stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode), 'untracked special file')
        sha = digest(os.readlink(path).encode() if path.is_symlink() else path.read_bytes()) if item['classification'] == 'backed-up' else ''
        result.append({**item, 'sha256': sha, 'mode': stat.S_IMODE(st.st_mode)})
    return result


def command_plan(p):
    repo = p['repo']
    def cmd(name, argv, cwd=repo, env=None):
        return {'id': name, 'argv': argv, 'cwd': cwd, 'env': env or BASE_ENV.copy(), 'timeout': p['command_timeout']}
    def g(name, *args):
        return cmd(name, git_argv(p, *args), env=git_env(p))
    def life(name, action):
        if action in ('stop', 'start', 'recover-start'):
            return cmd(name, [SYSTEMCTL, '--user', 'stop' if action == 'stop' else 'start', p['unit']],
                       p['installation'], env=system_env())
        return p['checks'][name]
    forward = [life('drain', 'drain'), life('stop', 'stop')]
    if p['operation'] == 'stage':
        forward += [g('switch', 'switch', 'staging'),
                    g('merge', 'merge', '--ff-only', p['candidate']['sha'])]
    elif p['operation'] == 'promote':
        forward += [g('switch', 'switch', 'main'), g('merge', 'merge', '--ff-only', p['candidate']['sha']),
                    g('push', 'push', p['remote'], p['candidate']['sha'] + ':refs/heads/main')]
    else:
        forward += [g('switch', 'switch', '--detach', p['rollback']['sha'])]
    forward += [life('offline', 'offline'), life('start', 'start'), life('smoke', 'smoke')]
    recovery = [life('recover-stop', 'stop'), g('recover-switch', 'switch', '--detach', p['rollback']['sha']),
                life('recover-offline', 'offline'), life('recover-start', 'recover-start'), life('recover-smoke', 'recover-smoke')]
    return forward, recovery


class Run:
    def __init__(self, packet, authority):
        self.path = Path(packet).absolute()
        require(not any(c in str(self.path) for c in '%$\n\r'), 'unsafe systemd packet path')
        self.state = private(self.path.parent, True)
        require(self.state.resolve() == self.state, 'canonical state path required')
        for entry in self.state.iterdir():
            private(entry)
        self.bytes = private(self.path).read_bytes()
        require(re.fullmatch('[a-f0-9]{64}', authority) and digest(self.bytes) == authority, 'authority digest mismatch')
        self.authority = authority
        self.p = loads(self.bytes)
        shape(self.p, SCHEMA)
        p = self.p
        require(p['version'] == 2 and p['operation'] in ('stage', 'promote', 'rollback'), 'unsupported packet')
        repo = Path(p['repo'])
        require(not any(c in p['installation'] for c in '%$\n\r'), 'unsafe systemd installation path')
        installation = private(p['installation'], True)
        require(repo.is_absolute() and repo.resolve() == repo, 'canonical repository required')
        require(installation.is_absolute() and installation.resolve() == installation, 'canonical installation required')
        require(not installation.is_relative_to(repo) and not self.state.is_relative_to(repo), 'control files inside switched source')
        require(Path(__file__).resolve() == installation / 'controller.py', 'run verified installed copy')
        require(Path(p['launcher']).is_absolute() and Path(p['launcher']).is_file(), 'launcher artifact required')
        require(Path(p['lock']).is_absolute() and not Path(p['lock']).resolve().is_relative_to(repo), 'external lock required')
        for unit in (p['unit'], p['controller_unit']):
            require(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.@-]*\.service', unit), 'exact service required')
        require(p['unit'] != p['controller_unit'], 'unit collision')
        require(p['drain_path'] == str(self.state / 'drain-proof.json') and p['health_path'] == str(self.state / 'runtime-health.json'), 'run-local proof paths required')
        require(p['order'].strip() and p['profile'].strip(), 'release order/profile missing')
        require(p['candidate']['branch'] == 'staging' and p['rollback']['branch'] == 'rollback', 'revision branches')
        require(p['model']['kind'] in ('fake', 'claude') and p['model']['name'] == 'claude-sonnet-5', 'invalid model config')
        require(1 <= p['model']['timeout'] <= 90, 'model timeout')
        require(1 <= p['command_timeout'] <= 300, 'command timeout')
        validate_route(p)
        for name, command in p['checks'].items():
            require(command['id'] == name, 'check identity')
            validate_check(command, p)
        require((p['commands'], p['recovery']) == command_plan(p), 'unapproved command/configuration')
        w = p['window']
        require(w['start'] < w['abort'] < w['expiry'] <= w['recovery_deadline'], 'window order')
        require(w['reserve'] >= sum(c['timeout'] for c in p['recovery']) + 40, 'insufficient recovery reserve')
        require(w['expiry'] - w['abort'] >= stop_budget(w) and w['recovery_deadline'] - w['abort'] >= stop_budget(w), 'reserve not protected')
        require(len({r['ref'] for r in p['extra_refs']}) == len(p['extra_refs']), 'duplicate extra ref')
        for ref in p['extra_refs']:
            require(re.fullmatch('[a-f0-9]{40}', ref['sha']), 'extra ref SHA')
        for rev in (p['current'], p['candidate'], p['rollback']):
            require(re.fullmatch('[a-f0-9]{40}', rev['sha']) and re.fullmatch('[a-f0-9]{40}', rev['tree']), 'commit/tree type')
        self.recovering = False
        self.lock_fd = None

    def log(self, event, **data):
        fd = os.open(self.state / 'journal.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'ab') as file:
            file.write(encoded({'time': time.time(), 'event': event, **data}))
            file.flush()
            os.fsync(file.fileno())

    def result(self, status, reason):
        command = shlex.join([PYTHON, str(Path(__file__).resolve()), str(self.path),
                              '--authority', self.authority, '--inspect-recovery'])
        durable(self.state / 'result.json', {'status': status, 'reason': reason,
                'authority': self.authority, 'time': time.time(), 'operator_command': command,
                'scope': 'externally-approved-packet', 'reconciliation': 'Never rewind published main; use a separately reviewed nonforce recovery commit.'})

    def gate(self, timeout=0):
        self.clock_gate(timeout)
        require(private(self.path).read_bytes() == self.bytes, 'frozen packet changed')
        self.receipts()
        self.artifacts()
        self.clock_gate(timeout)

    def clock_gate(self, timeout=0):
        w = self.p['window']
        now = time.time()
        deadline = w['recovery_deadline'] if self.recovering else min(w['abort'], w['expiry'] - w['reserve'])
        require(math.isfinite(now) and (self.recovering or w['start'] <= now) and now + timeout + 2 < deadline, 'command deadline exhausted')

    def receipts(self):
        for name, ref in self.p['receipts'].items():
            path = Path(ref['path'])
            require(path == self.state / (name + '.json'), 'receipt path')
            data = private(path).read_bytes()
            require(digest(data) == ref['sha256'], 'receipt drift')
            receipt = loads(data)
            shape(receipt, {'order': str, 'kind': str, 'candidate': str, 'rollback': str,
                            'repo': str, 'unit': str, 'profile': str,
                            'expires': int, 'evidence': str, 'approved': bool, 'facts': 'environment'})
            require(receipt['order'] == self.p['order'] and receipt['kind'] == name and receipt['approved'] is True,
                    'receipt not approved')
            require(receipt['candidate'] == self.p['candidate']['sha'] and receipt['rollback'] == self.p['rollback']['sha'], 'receipt SHA mismatch')
            require(receipt['expires'] >= self.p['window']['recovery_deadline'] and time.time() < receipt['expires'], 'receipt expired')
            require(receipt['evidence'].strip(), 'receipt evidence missing')
            require(all(receipt[k] == self.p[k] for k in ('repo', 'unit', 'profile')), 'receipt target mismatch')
            facts = receipt['facts']
            required = {'backup': ('coverage', 'restore_verified'),
                        'compatibility': ('candidate', 'rollback', 'dependencies', 'persistent_state'),
                        'drain': ('mechanism', 'all_source_consumers'),
                        'sole_writer': ('owner', 'hold', 'writers', 'recovery_handoff'),
                        'git_credentials': ('route', 'actor', 'permission_verified'),
                        'authority': ('approver', 'provenance', 'batch', 'recovery', 'contact'),
                        'ci': ('exact_sha', 'result'), 'review': ('exact_sha', 'verdict'),
                        'readiness': ('probe_path', 'probe_sha256', 'auth_method', 'model', 'cli_sha256')}[name]
            require(set(facts) == set(required) and all(facts.values()), 'receipt facts missing')
            if name in ('ci', 'review'):
                require(facts['exact_sha'] == self.p['candidate']['sha'], 'qualification SHA mismatch')
            if name == 'readiness' and self.p['model']['kind'] == 'claude':
                require(facts['auth_method'] == 'claude.ai' and facts['model'] == self.p['model']['name'], 'readiness identity')
                require(digest(Path(CLAUDE).read_bytes()) == facts['cli_sha256'], 'readiness executable drift')
                proof = private(facts['probe_path']).read_bytes()
                require(digest(proof) == facts['probe_sha256'], 'readiness evidence drift')
                probe_data = loads(proof)
                require(probe_data['authMethod'] == 'claude.ai' and probe_data['result'] == 'RELEASE_CONTROLLER_READY'
                        and probe_data['tools'] == [] and probe_data.get('is_error', False) is False, 'readiness not proven')
                accounting_models(probe_data['models'], self.p['model']['name'])
            if name == 'git_credentials':
                require(facts['route'] == digest(encoded(self.p['git_route'])), 'credential route receipt mismatch')

    def artifacts(self):
        expected = {str(Path(__file__).resolve()), self.p['runbook']['path'], self.p['launcher'],
                    GIT, SYSTEMCTL, PYTHON, '/usr/bin/systemd-run', self.p['baseline']['argv'][0]}
        unit = show(self.p['unit'])
        require(unit['NeedDaemonReload'] == 'no', 'service definition not loaded')
        expected.update([unit['FragmentPath'], *shlex.split(unit['DropInPaths'])])
        for argv in ([GIT], [SYSTEMCTL], ['/usr/bin/systemd-run'], [PYTHON, str(Path(__file__).resolve())]):
            expected.update(command_files(argv))
        expected.update(baseline_files(self.p['baseline']['argv']))
        require(digest(raw([SYSTEMCTL, '--user', 'cat', self.p['unit']], '/', env=system_env()).encode())
                == self.p['unit_definition_sha256'], 'service definition drift')
        for command in self.p['checks'].values():
            expected.update(command_files(command['argv']))
        for key in ('helper', 'ssh'):
            if self.p['git_route'][key]:
                expected.update(command_files([self.p['git_route'][key]]))
        if self.p['model']['kind'] == 'claude':
            expected.update(command_files([CLAUDE]))
        require(self.p['launcher'] in baseline_files(self.p['baseline']['argv']),
                'legacy baseline requires separately approved bootstrap installation and restart')
        if self.p['model']['kind'] == 'claude' or Path(self.p['launcher']).name == 'launch_gateway.py':
            argv = self.p['baseline']['argv']
            require(argv[1:6] == ['-I', '-S', '-B', '-X', 'pycache_prefix=/dev/null'], 'isolated launcher argv required')
            require('--dependencies' in argv and '--dependencies-sha256' in argv, 'dependency pin required')
            require(len(argv) == 20 and argv[6] == self.p['launcher']
                    and argv[7:9] == ['--repo', self.p['repo']] and argv[9] == '--startup-json'
                    and argv[11] == '--dependencies' and argv[13] == '--dependencies-sha256'
                    and argv[15:] == ['--', '-m', 'hermes_cli.main', 'gateway', 'run'], 'exact launcher argv required')
            require(Path(argv[10]).is_absolute() and not Path(argv[10]).is_relative_to(Path(self.p['repo'])), 'startup evidence path')
            dependency_path = Path(argv[argv.index('--dependencies') + 1])
            dependency_sha = argv[argv.index('--dependencies-sha256') + 1]
            require(digest(dependency_path.read_bytes()) == dependency_sha, 'dependency manifest drift')
            dependencies = loads(dependency_path.read_bytes())
            expected.update({str(dependency_path), str(Path(self.p['launcher']).with_name('runtime_observation.py'))})
            expected.update(dependencies['files'])
            expected.update(str(Path(self.p['launcher']).with_name(name))
                            for name in ('health.py', 'runtime_health.py', 'drain_proof.py'))
            require(set(dependencies) == {'paths', 'files', 'observer_sha256', 'venv_config'}, 'dependency manifest shape')
            actual_dependencies = {}
            for root in dependencies['paths']:
                root = Path(root)
                require(root.is_absolute() and root.resolve() == root and root.is_dir(), 'dependency root path')
                for file in root.rglob('*'):
                    if '__pycache__' in file.parts or file.suffix == '.pyc':
                        continue
                    if file.is_file():
                        require(not file.is_symlink(), 'dependency symlink unsupported')
                        actual_dependencies[str(file)] = digest(file.read_bytes())
            selected_prefix = Path(argv[0]).parent.parent.resolve()
            venv_config = dependencies['venv_config']
            require(venv_config == (str(selected_prefix / 'pyvenv.cfg') if (selected_prefix / 'pyvenv.cfg').is_file() else ''), 'venv config does not match interpreter')
            if venv_config:
                actual_dependencies[venv_config] = digest(Path(venv_config).read_bytes())
            for revision_name in ('current', 'candidate', 'rollback'):
                require(not ({str(Path(self.p['repo']) / item['path']) for item in self.p[revision_name]['files']} & actual_dependencies.keys()), 'tracked source classified as dependency')
            require(actual_dependencies == dependencies['files'], 'dependency bytes drift')
            require(dependencies['observer_sha256'] == digest(Path(self.p['launcher']).with_name('runtime_observation.py').read_bytes()), 'observer pin drift')
        manifest = {a['path']: a for a in self.p['artifacts']}
        require(len(manifest) == len(self.p['artifacts']) and expected <= manifest.keys(), 'installation/executable manifest')
        for path, artifact in manifest.items():
            file = Path(path)
            require(file.is_absolute() and file.is_file(), 'artifact path')
            st = file.stat()
            require(artifact_uid(file) == artifact['uid'] and stat.S_IMODE(st.st_mode) == artifact['mode']
                    and not st.st_mode & 0o022, 'unsafe artifact permissions')
            require(digest(file.read_bytes()) == artifact['sha256'], 'installed artifact drift')
        require(digest(private(self.p['runbook']['path']).read_bytes()) == self.p['runbook']['sha256'], 'frozen runbook drift')
        require(digest(Path(self.p['launcher']).read_bytes()) == self.p['launcher_sha256'], 'launcher drift')

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
            self.log('lock-acquired', inode=self.lock_identity[1], coordination='external sole-writer receipt; lock coordinates only this runner')
            yield
        finally:
            self.lock_fd = None
            os.close(fd)

    def locked(self):
        require(self.lock_fd is not None, 'lock not held')
        st = (Path(self.p['lock'])).stat()
        require((st.st_dev, st.st_ino) == self.lock_identity, 'lock inode changed')

    def clean(self):
        repo = self.p['repo']
        # No checkout hooks, filters, includes, shell credentials, external diff or executable config.
        config = git(repo, 'config', '--local', '--list')
        require(config.splitlines() == self.p['git_config'], 'local git configuration drift')
        for line in config.splitlines():
            key = line.split('=', 1)[0]
            require(key in ('core.repositoryformatversion', 'core.filemode', 'core.bare', 'core.logallrefupdates', 'user.name', 'user.email') or re.fullmatch(r'(remote\.[^.]+\.(url|pushurl|fetch)|branch\..+\.(remote|merge)|submodule\..+\.(active|url))', key), 'unsafe local git config')
        require(git(repo, 'status', '--porcelain=v1', '--untracked-files=no') == '', 'dirty tracked source')
        require(untracked(repo, self.p['untracked']) == self.p['untracked'], 'untracked/ignored inventory drift')
        require(git(repo, 'rev-parse', '--show-toplevel') == repo, 'repository path mismatch')
        st = Path(repo).stat()
        require({'device': st.st_dev, 'inode': st.st_ino} == self.p['repo_identity'], 'repository identity drift')
        for worktree in git(repo, 'worktree', 'list', '--porcelain').strip().split('\n\n'):
            lines = worktree.splitlines()
            if lines[0] != 'worktree ' + repo:
                require(not any(x in lines for x in ('branch refs/heads/main', 'branch refs/heads/staging', 'branch refs/heads/rollback')), 'target branch in another worktree')
        require(not (Path(repo) / '.git/index.lock').exists(), 'index lock present')
        for path in (Path(repo), Path(repo) / '.git'):
            require(path.stat().st_uid == os.getuid() and not path.is_symlink() and path.stat().st_mode & 0o022 == 0, 'unsafe repository permissions')  # windows-footgun: ok

    def refs(self, promoted=False, staged=False):
        p, repo = self.p, self.p['repo']
        require(set(git(repo, 'for-each-ref', '--format=%(refname)').splitlines()) == {'refs/heads/main', 'refs/heads/staging', 'refs/heads/rollback'} | {r['ref'] for r in p['extra_refs']}, 'unclassified ref drift')
        self.extra_refs()
        for branch, expected in p['local'].items():
            if promoted and branch == 'main':
                expected = p['candidate']['sha']
            if staged and branch == 'staging':
                expected = p['candidate']['sha']
            require(git(repo, 'rev-parse', 'refs/heads/' + branch) == expected, 'local ref drift')
        self.remote(promoted)

    def remote(self, promoted=False, ambiguous=False):
        self.clock_gate(10)
        p = self.p
        refs = dict(line.split()[::-1] for line in raw(git_argv(p, 'ls-remote', '--refs', p['remote'], 'refs/heads/main', 'refs/heads/staging'), p['repo'], env=git_env(p)).splitlines())
        allowed_main = {p['authoritative']['main']}
        if promoted or ambiguous:
            allowed_main.add(p['candidate']['sha'])
        require(refs.keys() == {'refs/heads/main', 'refs/heads/staging'}, 'authoritative refs missing')
        require(refs['refs/heads/main'] in allowed_main and refs['refs/heads/staging'] == p['authoritative']['staging'], 'authoritative ref drift')
        if promoted and not ambiguous:
            require(refs['refs/heads/main'] == p['candidate']['sha'], 'promotion not published')
        self.log('authoritative-readback', refs=refs)
        return refs

    def bytes_at(self, rev, branch=None):
        self.clean()
        repo = self.p['repo']
        require(git(repo, 'rev-parse', 'HEAD') == rev['sha'], 'HEAD mismatch')
        actual = git(repo, 'symbolic-ref', '-q', '--short', 'HEAD') if (Path(repo) / '.git/HEAD').read_text(encoding='utf-8').startswith('ref:') else ''
        require(actual == (rev['branch'] if branch is None else branch), 'branch mismatch')
        require(git(repo, 'write-tree') == rev['tree'] == git(repo, 'rev-parse', 'HEAD^{tree}'), 'index/tree mismatch')
        require(revision(repo, rev['sha'], rev['branch']) == rev, 'frozen critical inventory mismatch')
        for item in rev['files']:
            path = Path(repo) / item['path']
            require(path.resolve().is_relative_to(Path(repo)) and not path.is_symlink(), 'source path escapes')
            require(digest(path.read_bytes()) == item['sha256'], 'working byte mismatch')
            require(bool(path.stat().st_mode & 0o111) == (item['mode'] == '100755'), 'working mode mismatch')

    def inactive(self):
        s = show(self.p['unit'])
        require(s['ActiveState'] in ('inactive', 'failed') and s['MainPID'] == '0', 'service not proven inactive')
        require(not cg_pids(s['ControlGroup'] or self.p['baseline']['cgroup']), 'service cgroup not empty')
        require(not Path('/proc', str(self.p['baseline']['pid'])).exists(), 'baseline process still exists')
        require(not cg_pids(self.p['baseline']['cgroup']), 'gateway cgroup not empty')
        self.drained()

    def topology(self, recovery=False):
        p = self.p
        s = show(p['controller_unit'])
        me = proc(os.getpid())
        require(me['cgroup'] == s['ControlGroup'] and s['ControlGroup'].endswith('/' + p['controller_unit']), 'not independently supervised')
        require(not (p['baseline']['cgroup'] == me['cgroup'] or me['cgroup'].startswith(p['baseline']['cgroup'] + '/')), 'gateway cgroup dependency')
        require(s['Restart'] == 'no' and s['KillMode'] == 'control-group'
                and s['KillSignal'] == '9' and s['FinalKillSignal'] == '9'
                and s['SendSIGKILL'] == 'yes' and s['TimeoutStopFailureMode'] == 'kill'
                and systemd_seconds(s['TimeoutStopUSec']) == p['window']['reserve']
                and not s.get('ExecStop'), 'unsafe supervisor lifecycle')
        for key in ('PartOf', 'BindsTo', 'PropagatesStopTo', 'StopPropagatedFrom'):
            require(not s.get(key), 'lifecycle coupling')
        require(set((s['Requires'] + ' ' + s['Wants']).split()) <= {'app.slice', 'basic.target'}, 'unapproved lifecycle dependencies')
        require(s['WorkingDirectory'] == str(self.state), 'supervisor cwd')
        require('--recover' in s['ExecStopPost'] and self.authority in s['ExecStopPost'], 'external recovery guard missing')
        if recovery:
            require(cg_pids(me['cgroup']) == {os.getpid()}, 'takeover fencing not proven')
        else:
            require(s['MainPID'] == str(os.getpid()), 'not supervisor MainPID')
        self.log('fenced' if recovery else 'ready', cgroup=me['cgroup'], pid=os.getpid(), restart=s['Restart'])
        if not recovery:
            durable(self.state / 'ready.json', {'cgroup': me['cgroup'], 'pid': os.getpid(), 'authority': self.authority})

    def preflight(self):
        self.gate(sum(c['timeout'] for c in self.p['commands']) + self.p['model']['timeout'] * len(self.p['commands']))
        self.bytes_at(self.p['current'])
        self.refs()
        for key in ('candidate', 'rollback'):
            rev = self.p[key]
            require(revision(self.p['repo'], rev['sha'], rev['branch']) == rev, 'revision drift')
        self.qualified_refs()
        require(show(self.p['unit'])['ActiveState'] == 'active', 'baseline inactive')
        require(proc(int(show(self.p['unit'])['MainPID'])) == self.p['baseline'], 'baseline identity changed')
        require(self.p['launcher'] in self.p['baseline']['argv'], 'launcher process mismatch')
        self.clock_gate()
        self.log('preflight-passed', model=self.p['model']['kind'], authority=self.authority)

    def qualified_refs(self):
        p = self.p
        require(p['local']['rollback'] == p['rollback']['sha'], 'rollback not preserved')
        require(p['candidate']['sha'] == p['authoritative']['staging'], 'candidate not qualified staging')
        if p['operation'] == 'stage':
            git(p['repo'], 'merge-base', '--is-ancestor', p['local']['staging'], p['candidate']['sha'])
        else:
            require(p['local']['staging'] == p['candidate']['sha'], 'candidate not qualified staging')
        if p['local']['main'] != p['authoritative']['main']:
            require(p['operation'] in ('stage', 'promote'), 'main alignment')
            # All three immutable commits must already exist locally. No fetch,
            # inferred parent, or ref-only alignment of a checked-out branch.
            def ancestor(left, right):
                return subprocess.run([GIT, '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                                       '-c', 'submodule.recurse=false', 'merge-base', '--is-ancestor',
                                       left, right],
                                      cwd=p['repo'], env=BASE_ENV, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).returncode == 0

            git(p['repo'], 'merge-base', '--is-ancestor', p['local']['main'], p['candidate']['sha'])
            git(p['repo'], 'merge-base', '--is-ancestor', p['authoritative']['main'], p['candidate']['sha'])
            require(ancestor(p['local']['main'], p['authoritative']['main'])
                    or ancestor(p['authoritative']['main'], p['local']['main']), 'main chain divergence')
        if p['operation'] == 'promote':
            git(p['repo'], 'merge-base', '--is-ancestor', p['authoritative']['main'], p['candidate']['sha'])

    def model_plan(self):
        plan = loads(self.bytes)
        for key in ('current', 'candidate', 'rollback'):
            inventory = plan[key]['files']
            plan[key]['files'] = {'count': len(inventory), 'sha256': digest(encoded(inventory))}
        for key in ('untracked', 'extra_refs', 'git_config'):
            inventory = plan[key]
            plan[key] = {'count': len(inventory), 'sha256': digest(encoded(inventory))}
        return {'packet_sha256': self.authority, 'validated_plan': plan}

    def propose(self, cmd):
        self.gate(self.p['model']['timeout'] + cmd['timeout'])
        self.locked()
        if self.p['model']['kind'] == 'fake':
            proposal = {'op': cmd['id']}
            self.log('model-proposal', simulated=True, op=proposal['op'])
        else:
            prompt = ('You are a bounded release executor, not approver. No tools. Read this entire frozen execution plan. '
                      'Inventory digests below bind the full inventories verified by the deterministic runner. '
                      'Choose exactly the next approved op ID or abort. Each choice drives one operation; '
                      'completed IDs are durable execution receipts. Return JSON {"op":"ID"} only.\n'
                      + encoded(self.model_plan()).decode() + '\nFrozen runbook (read in full):\n'
                      + private(self.p['runbook']['path']).read_text(encoding='utf-8') + '\nCompleted journal:\n'
                      + (self.state / 'journal.jsonl').read_text(encoding='utf-8') + '\nNext operation: ' + cmd['id'])
            output = raw(claude_argv(prompt), self.state, self.p['model']['timeout'], claude_env())
            result = loads(output)
            inference_accounting(result, self.p['model']['name'])
            proposal = loads(result['result'])
            self.log('model-proposal', simulated=False, models=sorted(result.get('modelUsage', {})))
        accept_proposal(proposal, cmd['id'])
        self.log('proposal-accepted', op=cmd['id'])

    def execute(self, cmd):
        self.gate(cmd['timeout'] + (10 if cmd['id'] == 'push' else 0))
        self.locked()
        self.log('intent', op=cmd['id'])
        if cmd['id'] in ('stop', 'recover-stop'):
            # Written BEFORE stop: crash in the stop/readback gap is recoverable.
            durable(self.state / 'stopped-intent.json', {'authority': self.authority})
            self.result('recovery-required', 'stop may have begun; deterministic guard pending')
        try:
            output = raw(cmd['argv'], cmd['cwd'], cmd['timeout'], cmd['env'])
            if cmd['id'] == 'drain':
                durable(self.p['drain_path'], loads(output))
                self.drained()
            if cmd['id'] in ('smoke', 'recover-smoke'):
                durable(self.p['health_path'], loads(output))
        except Exception:
            if cmd['id'] == 'push':
                self.remote(ambiguous=True)
            raise
        self.log('done', op=cmd['id'])
        self.gate()

    def verify_runtime(self, rev):
        self.gate(5)
        status = show(self.p['unit'])
        require(status['ActiveState'] == 'active', 'startup inactive')
        actual = proc(int(status['MainPID']))
        require(actual['pid'] != self.p['baseline']['pid'] and actual['starttime'] != self.p['baseline']['starttime'], 'runtime not new')
        require(actual['argv'] == self.p['baseline']['argv'] and actual['cgroup'] == self.p['baseline']['cgroup'], 'runtime identity mismatch')
        health = loads(private(self.p['health_path']).read_bytes())
        shape(health, {'pid': int, 'starttime': str, 'sha': str, 'source': str, 'bytes': REV['files'],
                       'loaded': LOADED_SOURCE, 'executable_sha256': str, 'healthy': bool,
                       'platform': str, 'scheduler': str, 'persistence': str, 'sessions': str})
        expected_loaded = {item['path']: item['sha256'] for item in rev['files']}
        require(health['loaded'] and all(item['path'] in expected_loaded and expected_loaded[item['path']] == item['sha256']
                for item in health['loaded']), 'loaded source evidence mismatch')
        require(health['pid'] == actual['pid'] and health['starttime'] == actual['starttime']
                and health['sha'] == rev['sha'] and health['source'] == self.p['repo']
                and health['bytes'] == rev['files'] and health['healthy'] is True
                and health['executable_sha256'] == digest(Path(actual['argv'][0]).read_bytes()), 'loaded source/health mismatch')
        require(all(health[k] == 'ok' for k in ('platform', 'scheduler', 'persistence', 'sessions')), 'application health incomplete')
        self.log('runtime-verified', pid=actual['pid'], sha=rev['sha'])

    def drained(self):
        proof = loads(private(self.p['drain_path']).read_bytes())
        shape(proof, {'active_jobs': int, 'unit': str, 'pid': int, 'starttime': str, 'observed': int})
        require(proof['active_jobs'] == 0 and proof['unit'] == self.p['unit']
                and proof['pid'] == self.p['baseline']['pid'] and proof['starttime'] == self.p['baseline']['starttime']
                and self.p['window']['start'] <= proof['observed'] <= time.time(), 'no bound drain proof')

    def extra_refs(self):
        for item in self.p['extra_refs']:
            require(item['ref'].startswith('refs/') and item['ref'] not in ('refs/heads/main', 'refs/heads/staging', 'refs/heads/rollback'), 'extra ref overlaps')
            require(git(self.p['repo'], 'rev-parse', '--verify', item['ref']) == item['sha'], 'extra ref drift')

    def forward(self):
        with self.lock():
            require(not (self.state / 'consumed.json').exists(), 'single-use packet already consumed')
            self.preflight()
            self.topology()
            self.gate()
            durable(self.state / 'consumed.json', {'authority': self.authority}, exclusive=True)
            promoted = False
            staged = False
            for cmd in self.p['commands']:
                self.gate()
                self.propose(cmd)
                if cmd['id'] == 'stop':
                    self.drained()
                    self.bytes_at(self.p['current'])
                    self.refs()
                    require(proc(int(show(self.p['unit'])['MainPID'])) == self.p['baseline'], 'baseline drift before stop')
                if cmd['id'] in ('switch', 'merge', 'push', 'offline'):
                    self.inactive()
                    self.clean()
                if cmd['id'] == 'switch':
                    self.refs()
                if cmd['id'] == 'merge':
                    parent_branch = 'staging' if self.p['operation'] == 'stage' else 'main'
                    self.refs()
                    self.bytes_at(revision(self.p['repo'], self.p['local'][parent_branch], parent_branch))
                if cmd['id'] == 'push':
                    self.bytes_at(self.p['candidate'], 'main')
                    self.remote()
                if cmd['id'] == 'start':
                    self.inactive()
                    target = self.p['rollback'] if self.p['operation'] == 'rollback' else self.p['candidate']
                    branch = '' if self.p['operation'] == 'rollback' else ('main' if promoted else 'staging')
                    self.bytes_at(target, branch)
                    self.refs(promoted, staged)
                self.execute(cmd)
                if cmd['id'] == 'stop':
                    self.inactive()
                if cmd['id'] == 'push':
                    promoted = True
                    self.remote(True)
                if cmd['id'] == 'merge' and self.p['operation'] == 'stage':
                    staged = True
                    self.bytes_at(self.p['candidate'], 'staging')
                    self.refs(staged=True)
            self.gate()
            self.verify_runtime(target)
            self.bytes_at(target, branch)
            self.refs(promoted, staged)
            self.result('succeeded', self.p['operation'] + '-active')

    def recovery_refs(self):
        refs = set(git(self.p['repo'], 'for-each-ref', '--format=%(refname)').splitlines())
        require(refs == {'refs/heads/main', 'refs/heads/staging', 'refs/heads/rollback'} | {r['ref'] for r in self.p['extra_refs']}, 'recovery ref inventory drift')
        self.extra_refs()
        # A killed Git command can have completed its atomic ref update without
        # a done journal entry. Accept only this operation's frozen endpoints.
        for branch, operation in (('main', 'promote'), ('staging', 'stage')):
            allowed = {self.p['local'][branch]}
            if self.p['operation'] == operation:
                allowed.add(self.p['candidate']['sha'])
            require(git(self.p['repo'], 'rev-parse', 'refs/heads/' + branch) in allowed, 'recovery ' + branch + ' drift')
        require(git(self.p['repo'], 'rev-parse', 'refs/heads/rollback') == self.p['rollback']['sha'], 'rollback ref drift')
        self.remote(ambiguous=self.p['operation'] == 'promote')

    def recover(self):
        self.recovering = True
        if (self.state / 'result.json').exists():
            status = loads(private(self.state / 'result.json').read_bytes())['status']
            if status in TERMINAL:
                return
        if not (self.state / 'stopped-intent.json').exists():
            self.result('preflight-blocked', 'supervisor ended before stop intent')
            return
        require(loads(private(self.state / 'stopped-intent.json').read_bytes()) == {'authority': self.authority}, 'stop intent belongs to another packet')
        self.result('recovery-required', 'recovery not yet proven; keep stopped if uncertain')
        with self.lock():
            self.gate(sum(c['timeout'] for c in self.p['recovery']))
            self.topology(recovery=True)
            self.log('recovery-begin', model_used=False)
            for cmd in self.p['recovery']:
                self.gate()
                if cmd['id'] != 'recover-stop':
                    if cmd['id'] != 'recover-smoke':
                        self.inactive()
                    self.clean()
                if cmd['id'] == 'recover-switch':
                    self.recovery_refs()
                if cmd['id'] == 'recover-start':
                    self.recovery_refs()
                    self.bytes_at(self.p['rollback'], '')
                self.execute(cmd)
            self.bytes_at(self.p['rollback'], '')
            self.verify_runtime(self.p['rollback'])
            self.recovery_refs()
            self.gate()
            self.result('rolled-back', 'local detached known-good; published main preserved')


def accounting_models(models, expected):
    # Only this auxiliary identity was observed in preserved shared-login output.
    require(type(models) is list and all(type(m) is str for m in models), 'malformed model accounting')
    require(len(set(models)) == len(models) and expected in models
            and set(models) <= {expected, 'claude-haiku-4-5-20251001'}, 'unexpected inference model')


def inference_accounting(result, expected):
    require(type(result) is dict and result.get('is_error') is False
            and type(result.get('result')) is str, 'Claude inference failed')
    usage = result.get('modelUsage')
    require(type(usage) is dict and all(type(v) is dict and all(
        type(v.get(k)) is int and v[k] >= 0 for k in ('inputTokens', 'outputTokens'))
        for v in usage.values()), 'malformed model accounting')
    accounting_models(list(usage), expected)


def systemd_seconds(value):
    units = {'us': 0.000001, 'ms': 0.001, 's': 1, 'min': 60, 'h': 3600, 'd': 86400}
    parts = value.split()
    require(parts and all(re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:us|ms|s|min|h|d)', x) for x in parts), 'invalid systemd timeout')
    return sum(float(re.match(r'[0-9]+(?:\.[0-9]+)?', x)[0]) * units[re.search(r'[a-z]+', x)[0]] for x in parts)


def stop_budget(window):
    # systemd applies TimeoutStopSec per termination phase AND to ExecStopPost.
    # Allow both kill waits, a separate complete recovery allowance, and dispatch
    # margin. No ExecStop command is permitted to add another termination phase.
    return 2 * window['reserve'] + window['reserve'] + 10


def accept_proposal(proposal, expected):
    shape(proposal, {'op': str})
    require(proposal['op'] == expected, 'model aborted/out-of-order/unapproved proposal')


def inspect_recovery(packet):
    # Diagnostics must still work when the packet or copied-artifact hash fails.
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
                      'next_action': 'Read exact unit and process state from frozen packet; inspect journal. Keep stopped on uncertainty. New authority required after repair; never reset/clean/force-push.'}))


def claude_env():
    # No inherited API keys, provider selection, CLAUDE_CONFIG_DIR or Git identity.
    return {'HOME': '/home/brian', 'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8'}


def claude_argv(prompt):
    return [CLAUDE, '--safe-mode', '--restricted', '-p', prompt, '--model', 'claude-sonnet-5',
            '--tools', '', '--permission-mode', 'dontAsk', '--output-format', 'json',
            '--no-session-persistence', '--strict-mcp-config', '--setting-sources', '',
            '--disable-slash-commands']


def probe(directory):
    directory = private(directory, True)
    help_text = raw([CLAUDE, '--help'], directory, 10, claude_env())
    for flag in ('--safe-mode', '--restricted', '--tools', '--permission-mode', '--output-format',
                 '--no-session-persistence', '--strict-mcp-config', '--setting-sources', '--disable-slash-commands'):
        require(flag in help_text, 'installed Claude capability missing')
    version = raw([CLAUDE, '--version'], directory, 10, claude_env())
    auth = loads(raw([CLAUDE, 'auth', 'status'], directory, 10, claude_env()))
    require(auth.get('loggedIn') is True and auth.get('authMethod') == 'claude.ai', 'shared subscription auth unavailable; no fallback')
    answer = loads(raw(claude_argv('Reply RELEASE_CONTROLLER_READY only. Do not use tools.'), directory, 60, claude_env()))
    require(answer.get('is_error') is False and answer.get('result') == 'RELEASE_CONTROLLER_READY', 'real inference refused/failed')
    inference_accounting(answer, 'claude-sonnet-5')
    durable(directory / 'probe.json', {'version': version, 'authMethod': 'claude.ai', 'result': answer['result'],
                                     'models': sorted(answer['modelUsage']), 'time': time.time(), 'tools': []})


def launch(run):
    p = run.p
    with run.lock():
        run.preflight()
        require(not (run.state / 'consumed.json').exists(), 'packet already consumed')
    argv = [PYTHON, str(Path(__file__).resolve()), str(run.path), '--authority', run.authority]
    # Immediate controller-only SIGKILL removes TERM handlers/stopped supervisors.
    # The guard must still prove no surviving cgroup writers before takeover.
    # No retry, no permanent unit. Same external sole-writer hold covers lock transfer.
    post = shlex.join(argv + ['--recover'])
    timeout = max(1, p['window']['recovery_deadline'] - int(time.time()))
    props = {'Type': 'exec', 'Restart': 'no', 'UMask': '0077', 'KillMode': 'control-group',
             'TimeoutStopSec': str(p['window']['reserve']), 'SendSIGKILL': 'yes',
             'KillSignal': 'SIGKILL', 'FinalKillSignal': 'SIGKILL', 'TimeoutStopFailureMode': 'kill',
             'WorkingDirectory': str(run.state), 'ExecStopPost': post,
             'RuntimeMaxSec': str(max(1, p['window']['abort'] - int(time.time()))),
             'StandardOutput': 'null', 'StandardError': 'null', 'NoNewPrivileges': 'yes',
             'ProtectControlGroups': 'yes'}
    command = ['/usr/bin/systemd-run', '--user', '--unit=' + p['controller_unit']]
    command += ['--property=' + k + '=' + v for k, v in props.items()]
    raw(command + argv + ['--supervise'], run.state, min(10, timeout), system_env())
    print(json.dumps({'unit': p['controller_unit'], 'result': str(run.state / 'result.json')}))


def main():
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] == 'probe':
        try:
            probe(Path(sys.argv[2]).absolute())
            print('real no-tools subscription probe passed; see private probe.json')
            return 0
        except Exception as exc:
            print('probe blocked: ' + (str(exc) if isinstance(exc, Refusal) else type(exc).__name__), file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('packet', type=Path)
    parser.add_argument('--authority', required=True, help='externally approved SHA256 of exact packet bytes')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='launch exact frozen release in an independent transient unit')
    mode.add_argument('--supervise', action='store_true', help=argparse.SUPPRESS)
    mode.add_argument('--recover', action='store_true', help=argparse.SUPPRESS)
    mode.add_argument('--inspect-recovery', action='store_true', help='non-mutating operator diagnostics')
    args = parser.parse_args()
    run = None
    try:
        if args.inspect_recovery:
            inspect_recovery(args.packet)
            return 0
        run = Run(args.packet, args.authority)
        if args.recover:
            run.recover()
        elif args.supervise:
            run.forward()
        elif args.apply:
            launch(run)
        else:
            with run.lock():
                run.preflight()
            print('preflight-passed (no lifecycle or source mutation performed)')
        return 0
    except Exception as exc:
        # No raw subprocess output, packet text, arbitrary exception messages or secrets.
        reason = str(exc) if isinstance(exc, Refusal) else type(exc).__name__
        if run is None:
            # Schema errors leave a private durable refusal without trusting packet paths.
            try:
                state = private(args.packet.absolute().parent, True)
                durable(state / 'result.json', {'status': 'recovery-required' if (state / 'stopped-intent.json').exists() else 'preflight-blocked', 'reason': reason, 'scope': 'externally-approved-packet', 'operator_command': shlex.join([PYTHON, str(Path(__file__).resolve()), str(args.packet.absolute()), '--authority', args.authority, '--inspect-recovery'])})
            except Exception:
                pass
        if run is not None:
            run.log('blocked', reason=reason)
            stopped = (run.state / 'stopped-intent.json').exists()
            if args.recover or not stopped:
                run.result('recovery-required' if stopped else 'preflight-blocked', reason)
        print('blocked: ' + reason, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
