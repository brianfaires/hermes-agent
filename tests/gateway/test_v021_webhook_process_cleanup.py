"""Retained payload transforms must reap their disposable process tree."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import psutil
from gateway.platforms.webhook_filters import WebhookRouteProcessor


class WebhookProcessCleanupTests(unittest.TestCase):
    def test_transform_still_accepts_json_and_rejects_outside_script_root(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'HERMES_HOME': root}):
            scripts = Path(root) / 'scripts'
            scripts.mkdir()
            (scripts / 'transform.py').write_text('import json,sys\np=json.load(sys.stdin)\np["transformed"]=True\nprint(json.dumps(p))\n')
            processor = WebhookRouteProcessor()
            self.assertEqual(processor.run_route_script('transform.py', {'message': 'test'}),
                             (True, {'message': 'test', 'transformed': True}))
            outside = Path(root) / 'outside.py'
            outside.write_text('raise RuntimeError("must never run")')
            self.assertEqual(processor.run_route_script(str(outside), {}), (False, None))

    def test_timeout_terminates_grandchild(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'HERMES_HOME': root}):
            scripts = Path(root) / 'scripts'
            scripts.mkdir()
            pidfile = scripts / 'child.pid'
            script = scripts / 'transform.py'
            script.write_text(
                'import subprocess,sys,time\nfrom pathlib import Path\n'
                'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"], '
                'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
                f'Path({str(pidfile)!r}).write_text(str(child.pid))\n'
                'time.sleep(30)\n'
            )
            child = None
            try:
                self.assertEqual(WebhookRouteProcessor(script_timeout_seconds=1).run_route_script('transform.py', {}), (False, None))
                self.assertTrue(pidfile.exists(), 'transform failed to spawn its child')
                child = psutil.Process(int(pidfile.read_text()))
                deadline = time.monotonic() + 3
                while child.is_running() and child.status() != psutil.STATUS_ZOMBIE and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(not child.is_running() or child.status() == psutil.STATUS_ZOMBIE,
                                'transform descendant survived timeout cleanup')
            except psutil.NoSuchProcess:
                pass
            finally:
                # Only this fixture's recorded process object can be signaled.
                if child is None and pidfile.exists():
                    try:
                        child = psutil.Process(int(pidfile.read_text()))
                    except psutil.NoSuchProcess:
                        pass
                if child is not None:
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass


if __name__ == '__main__':
    unittest.main()
