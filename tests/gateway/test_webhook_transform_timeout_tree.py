"""Real webhook timeouts stop owned descendants and preserve unrelated processes."""
import json
import os
import subprocess
import sys
import textwrap

import psutil
import pytest

from gateway.platforms.webhook_filters import WebhookRouteProcessor
from hermes_constants import get_hermes_home
from tests.waiting import wait_until


def _live(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _clean_owned(receipt):
    if not receipt.exists():
        return
    for record in json.loads(receipt.read_text(encoding='utf-8-sig')).get('processes', []):
        try:
            process = psutil.Process(record['pid'])
            if process.create_time() == record['created']:
                process.kill()
        except psutil.NoSuchProcess:
            pass


# The red-on-base case deliberately orphans a fixture child. Cleanup may
# signal it only while its recorded PID AND creation time still match.
@pytest.mark.live_system_guard_bypass
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('parent_exits', [False, True, 'reaped', 'detached-child'])
def test_timeout_stops_descendants_even_after_parent_exits(tmp_path, monkeypatch, parent_exits):
    scripts = get_hermes_home() / 'scripts'
    scripts.mkdir(parents=True, exist_ok=True)
    receipt = tmp_path / 'owned.json'
    script = scripts / 'tree.py'
    script.write_text(textwrap.dedent(f'''
        import json, os, pathlib, subprocess, sys, threading
        import psutil
        child = subprocess.Popen([sys.executable, '-c', 'import threading; threading.Event().wait(60)'],
                                 start_new_session={parent_exits == 'detached-child'!r})
        pathlib.Path({str(receipt)!r}).write_text(json.dumps({{
            'group': os.getpgrp(),
            'processes': [{{'pid': p.pid, 'created': p.create_time()}}
                          for p in (psutil.Process(), psutil.Process(child.pid))]
        }}))
        if {parent_exits in (True, 'reaped')!r}:
            sys.exit(0)
        threading.Event().wait(60)
    '''), encoding='utf-8')
    if parent_exits == 'reaped':
        from hermes_cli.local_runtime import processes
        original_spawn = processes.spawn_server

        def reap_launcher(*args, **kwargs):
            process, job = original_spawn(*args, **kwargs)
            process.wait(timeout=5)
            return process, job

        monkeypatch.setattr(processes, 'spawn_server', reap_launcher)
    unrelated = subprocess.Popen([sys.executable, '-c', 'import threading; threading.Event().wait(60)'])
    try:
        accepted, transformed = WebhookRouteProcessor(script_timeout_seconds=2).run_route_script(str(script), {})
        assert not accepted and transformed is None
        assert receipt.exists(), 'transform never reached its child spawn'
        evidence = json.loads(receipt.read_text(encoding='utf-8-sig'))
        child = evidence['processes'][1]['pid']
        wait_until(lambda: not _live(child), timeout=5, what='owned transform descendant termination')
        assert evidence['group'] == evidence['processes'][0]['pid']
        assert evidence['group'] != os.getpgrp()
        assert unrelated.poll() is None
    finally:
        _clean_owned(receipt)
        unrelated.terminate()
        unrelated.wait(timeout=5)
