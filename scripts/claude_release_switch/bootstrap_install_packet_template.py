#!/usr/bin/env python3
"""Build a frozen production bootstrap packet from exact reviewed inputs.

This helper performs no lifecycle mutation. It materializes exact command
records for the bootstrap guard and prints the packet plus its SHA256 for
separate Ang approval. When unit-definition validation is enabled, the reviewed
input must include both baseline_unit_sha256 and candidate_unit_sha256.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import stat
import sys

import bootstrap_recovery as bootstrap
from controller import PYTHON, SYSTEMCTL, artifact_uid, digest, encoded, loads, require, system_env


def lifecycle(id_, unit, installation, timeout, action=None):
    return {'id': id_, 'kind': 'command',
            'argv': [SYSTEMCTL, '--user', action or id_, unit],
            'cwd': installation, 'env': system_env(), 'timeout': timeout}


def command(id_, argv, installation, timeout):
    return {'id': id_, 'kind': 'command', 'argv': argv, 'cwd': installation,
            'env': system_env(), 'timeout': timeout}


def _script(installation, name):
    return str(Path(installation).resolve(strict=True) / name)


def _add_artifacts(packet, paths):
    artifacts = {entry['path']: entry for entry in packet.get('artifacts', [])}
    for path in paths:
        file = Path(path)
        st = file.stat()
        artifacts[str(file)] = {'path': str(file), 'sha256': digest(file.read_bytes()),
                                'uid': artifact_uid(file),
                                'mode': stat.S_IMODE(st.st_mode)}
    packet['artifacts'] = [artifacts[path] for path in sorted(artifacts)]


def build_packet(data):
    packet = dict(data)
    bindings = packet.pop('bootstrap_bindings')
    timeout = packet['command_timeout']
    installation = packet['installation']
    unit = packet['unit']
    max_age = int(bindings.get('max_age', 30))
    runtime_health_max_age = 90
    drain_timeout = int(bindings.get('drain_timeout', timeout))
    health_timeout = int(bindings.get('health_timeout', max(timeout, 120)))
    startup_timeout = int(bindings.get('startup_timeout', 90))
    recover_health_startup_timeout = health_timeout
    repeat_delay = str(float(bindings.get('repeat_delay', 0.2)))
    require(1 <= max_age <= 90, 'bootstrap max_age out of bounds')
    require(1 <= drain_timeout <= 300, 'bootstrap drain_timeout out of bounds')
    require(1 <= health_timeout <= 300, 'bootstrap health_timeout out of bounds')
    require(0 <= startup_timeout <= 240, 'bootstrap startup_timeout out of bounds')
    require(0.0 <= float(repeat_delay) <= 5.0, 'bootstrap repeat_delay out of bounds')
    hermes_home = str(Path(bindings['hermes_home']).resolve(strict=True))
    hold_json = str(Path(bindings['hold_json']).resolve(strict=True))
    startup_json = str(Path(bindings['startup_json']).resolve(strict=False))
    live_json = str(Path(bindings['live_json']).resolve(strict=False))
    repo = str(Path(bindings['repo']).resolve(strict=True))
    runtime_health = _script(installation, 'runtime_health.py')
    health = _script(installation, 'health.py')
    drain_proof = _script(installation, 'drain_proof.py')
    bootstrap_bindings = _script(installation, 'bootstrap_runtime_bindings.py')
    packet['checks'] = {
        'reload': {'id': 'reload', 'kind': 'command',
                   'argv': [SYSTEMCTL, '--user', 'daemon-reload'],
                   'cwd': installation, 'env': system_env(), 'timeout': timeout},
        'drain': command('drain',
                         [PYTHON, bootstrap_bindings, 'legacy-drain', hermes_home, hold_json,
                          repo, unit, str(packet['baseline']['pid']),
                          packet['baseline']['starttime'], '--recovery-deadline',
                          str(packet['window']['recovery_deadline']), '--max-age',
                          str(max_age), '--repeat-delay', repeat_delay],
                         installation, drain_timeout),
        'stop': lifecycle('stop', unit, installation, timeout),
        'clear-drain': command('clear-drain',
                               [PYTHON, bootstrap_bindings, 'clear-drain', hermes_home],
                               installation, drain_timeout),
        'start': lifecycle('start', unit, installation, timeout),
        'health': command('health',
                          [PYTHON, runtime_health, startup_json, live_json, unit,
                           hermes_home, '--max-age', str(runtime_health_max_age),
                           '--startup-timeout', str(startup_timeout)],
                          installation, health_timeout),
        'recover-stop': lifecycle('recover-stop', unit, installation, timeout, 'stop'),
        'recover-start': lifecycle('recover-start', unit, installation, timeout, 'start'),
        'recover-health': command('recover-health',
                                  [PYTHON, bootstrap_bindings, 'legacy-health', hermes_home,
                                   unit, json.dumps(packet['legacy']['argv'],
                                                    sort_keys=True, separators=(',', ':')),
                                   packet['legacy']['source'], 'legacy',
                                   '--max-age', str(max_age),
                                   '--startup-timeout', str(recover_health_startup_timeout)],
                                  installation, health_timeout),
    }
    packet['commands'], packet['recovery'] = bootstrap.command_plan(packet)
    _add_artifacts(packet, [runtime_health, health, drain_proof, bootstrap_bindings])
    return packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path,
                        help='private exact-input JSON; all hashes, paths, windows and receipts already reviewed')
    args = parser.parse_args()
    packet = build_packet(loads(args.input.read_bytes()))
    data = encoded(packet)
    sys.stdout.buffer.write(data)
    print('packet_sha256=' + digest(data), file=sys.stderr)
    print('authority=UNRESOLVED_ANG_APPROVAL_OF_PACKET_SHA256', file=sys.stderr)


if __name__ == '__main__':
    main()
