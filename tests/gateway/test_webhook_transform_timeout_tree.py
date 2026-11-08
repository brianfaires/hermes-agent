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
# parent_exits=True is Linux-only: once the launcher has exited, macOS refuses getpgid() on the
# unreaped leader, so upstream's kill_process_tree can no longer signal the group it led (the
# fork's own killpg sweep for this case was dropped by brianfaires/hermes-shared#62).
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize('parent_exits', [
    pytest.param(False, marks=pytest.mark.platforms('posix')),
    pytest.param(True, marks=pytest.mark.platforms('linux')),
    pytest.param('detached-child', marks=pytest.mark.platforms('posix')),
])
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
        if {parent_exits is True!r}:
            sys.exit(0)
        threading.Event().wait(60)
    '''), encoding='utf-8')
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
