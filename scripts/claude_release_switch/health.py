#!/usr/bin/python3
"""Read-only health contract: combine startup source evidence with fresh live checks.

The startup producer must capture what the process loaded at startup. This hook
never derives loaded source from current Git HEAD. Both inputs are operator-trusted
private JSON; it does not manufacture connectivity or persistence evidence.
"""
import json
from pathlib import Path
import sys
import time

from controller import REV, loads, private, proc, require, shape, show


def read_health(startup, live, unit):
    started = loads(private(startup).read_bytes())
    shape(started, {'pid': int, 'starttime': str, 'sha': str, 'source': str, 'bytes': REV['files'], 'executable_sha256': str})
    checked = loads(private(live).read_bytes())
    shape(checked, {'pid': int, 'starttime': str, 'observed': int,
                    'platform': str, 'scheduler': str, 'persistence': str, 'sessions': str})
    status = show(unit)
    require(status['ActiveState'] == 'active', 'service inactive')
    actual = proc(int(status['MainPID']))
    for record in (started, checked):
        require(record['pid'] == actual['pid'] and record['starttime'] == actual['starttime'], 'health process mismatch')
    require(time.time() - 30 <= checked['observed'] <= time.time(), 'stale health observation')
    require(Path(started['source']).is_absolute(), 'startup source path')
    fields = {key: checked[key] for key in ('platform', 'scheduler', 'persistence', 'sessions')}
    require(all(value == 'ok' for value in fields.values()), 'health check failed')
    return {**started, **fields, 'healthy': True}


if __name__ == '__main__':
    try:
        require(len(sys.argv) == 4, 'usage: health.py STARTUP_JSON LIVE_JSON EXACT_UNIT')
        print(json.dumps(read_health(*sys.argv[1:])))
    except Exception:
        print('health contract refused; evidence incomplete or stale', file=sys.stderr)
        sys.exit(2)
