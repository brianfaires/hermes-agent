"""Job completion survives a cached constants module from before native6f88751a7e."""
from pathlib import Path
import os
import subprocess
import sys

import pytest


_FINISH_WITH_CACHED_CONSTANTS = r'''
import importlib
import json
import sys
import importlib.util
from datetime import datetime, timedelta, timezone

import cron

# The released module's timing contract predates the shared recurrence helper.
spec = importlib.util.spec_from_file_location('cron.constants', sys.argv[2])
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)
sys.modules['cron.constants'] = legacy
cron.constants = legacy
for name in ('cron.unreachable_retry', 'cron.scheduler_tick'):
    sys.modules.pop(name, None)
    importlib.import_module(name)

from cron.jobs import create_job, get_job, mark_job_run
once = sys.argv[1] == 'once'
schedule = ((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            if once else 'every 24h')
job = create_job('finish across source update', schedule, repeat=1 if once else 2)
assert mark_job_run(job['id'], True)
record = get_job(job['id'])
assert record['last_status'] == 'ok', record
assert record['repeat']['completed'] == 1, record
assert record['state'] == ('completed' if once else 'scheduled'), record
assert (record['next_run_at'] is None) == once, record
print(json.dumps({'finished': True, 'kind': sys.argv[1]}))
'''


@pytest.mark.parametrize('kind', ['once', 'recurring'])
def test_finish_run_with_pre_update_constants_cached(tmp_path: Path, kind: str) -> None:
    result = subprocess.run(
        [sys.executable, '-c', _FINISH_WITH_CACHED_CONSTANTS, kind,
         str(Path(__file__).with_name('fixtures') / 'pre_recurrence_constants.py')],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, 'HERMES_HOME': str(tmp_path / 'home'),
             'HERMES_RUNTIME_DIR': str(tmp_path / 'runtime')},
        capture_output=True, text=True, timeout=25, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
