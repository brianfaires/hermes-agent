"""Simulated application for controller Git/systemd wire tests ONLY.

Real source provenance and GatewayRunner coverage live in test_release_actual_path.
This fixture receipt never qualifies a production launcher.
"""
import argparse
import importlib
import os
from pathlib import Path
import sys

import controller as c

parser = argparse.ArgumentParser()
parser.add_argument('--repo', type=Path)
parser.add_argument('--startup-json', type=Path)
parser.add_argument('argv', nargs=argparse.REMAINDER)
args = parser.parse_args()
sys.path.insert(0, str(args.repo))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
os.environ['PYTHONPYCACHEPREFIX'] = str(args.startup_json.parent.parent / 'pycache')
sys.dont_write_bytecode = True
sys.pycache_prefix = os.environ['PYTHONPYCACHEPREFIX']
module = importlib.import_module('hermes_cli.main')
sha = c.git(args.repo, 'rev-parse', 'HEAD')
rev = c.revision(args.repo, sha, '')
actual = c.proc(os.getpid())
c.durable(args.startup_json, {'pid': os.getpid(), 'starttime': actual['starttime'],
          'sha': sha, 'source': str(args.repo), 'bytes': rev['files'],
          'loaded': [{'module': 'hermes_cli.main', 'path': 'hermes_cli/main.py',
                      'sha256': c.digest(Path(module.__file__).read_bytes())}],
          'executable_sha256': c.digest(Path(sys.executable).read_bytes())})
sys.argv = [module.__file__, 'gateway', 'run']
module.main()
