"""Bookkeeping survives a release update with the old constants module cached."""
from pathlib import Path
import os
import subprocess
import sys

import pytest


_FINISH_WITH_CACHED_CONSTANTS = r'''
import importlib
import json
import sys
import types
from datetime import datetime, timedelta, timezone

import cron

# The released module's timing contract predates the shared recurrence helper.
legacy = types.ModuleType('cron.constants')
legacy.FIRE_CLAIM_TTL_SECONDS = 300
legacy.FIRE_CLAIM_SKEW_SECONDS = 60
legacy.CLAIM_TTL_INACTIVITY_HEADROOM = 3
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
        [sys.executable, '-c', _FINISH_WITH_CACHED_CONSTANTS, kind],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, 'HERMES_HOME': str(tmp_path / 'home'),
             'HERMES_RUNTIME_DIR': str(tmp_path / 'runtime')},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
