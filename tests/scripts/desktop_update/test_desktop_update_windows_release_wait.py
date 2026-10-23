"""R6 release waiting has its own native per-file execution budget."""
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from tests.scripts.desktop_update.test_desktop_update_windows_commit_outcome import _handoff
from tests.waiting import wait_until


_CHECKOUT_HOLDER = """
import msvcrt, os, sys
from pathlib import Path
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o644)
os.lseek(fd, 1 << 20, os.SEEK_SET)
msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)   # hermes_cli/update_lock.py::_try_lock's byte
Path(sys.argv[2]).touch()
sys.stdin.read(1)
"""


@pytest.mark.platforms('windows')
def test_result_finished_at_is_stamped_after_the_r6_release_wait(tmp_path: Path) -> None:
    # The relaunched Desktop drops a non-manual result whose finished_at is 30 minutes old; the
    # R6 wait lasts up to 2 h. The result must carry the time the hand-off actually finished.
    holders, releasers, released_at, errors = [], [], [], []
    stopped = threading.Event()

    def hold_checkout(home: Path, install: Path) -> None:
        ready = tmp_path / 'checkout-lock-ready'
        proc = subprocess.Popen([sys.executable, '-c', _CHECKOUT_HOLDER,
                                 str(install / '.hermes-update.lock'), str(ready)],
                                stdin=subprocess.PIPE, text=True)
        holders.append(proc)
        release_pipe = proc.stdin
        assert release_pipe is not None
        wait_until(lambda: ready.exists() or proc.poll() is not None, timeout=30, what='checkout lock acquisition')
        assert ready.exists(), 'checkout lock holder exited before acquiring the lock'
        log = home / 'logs/desktop-update-handoff.log'

        def release_after_wait() -> None:
            try:
                wait_until(lambda: stopped.is_set() or (log.exists() and 'keeping the update marker' in
                           log.read_text(encoding='utf-8-sig', errors='replace')), timeout=100, what='R6 release wait')
                if stopped.is_set():
                    return
                observed_second = int(time.time())
                # finished_at has whole-second resolution; distinguish it from the pre-wait value.
                wait_until(lambda: stopped.is_set() or int(time.time()) > observed_second + 1, timeout=5)
                if not stopped.is_set():
                    released_at.append(int(time.time()))
                    release_pipe.write('\n')
                    release_pipe.flush()
            except Exception as exc:
                errors.append(exc)
            finally:
                release_pipe.close()

        releaser = threading.Thread(target=release_after_wait)
        releasers.append(releaser)
        releaser.start()
    try:
        code, out, _, result, home = _handoff(tmp_path, '-NoGateway', prepare=hold_checkout)
    finally:
        stopped.set()
        for releaser in releasers:
            releaser.join(timeout=10)
        for proc in holders:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
    assert code == 0, out
    assert not errors, errors
    log = (home / 'logs/desktop-update-handoff.log').read_text(encoding='utf-8-sig')
    assert 'keeping the update marker' in log, log
    assert released_at and result['ok'] is True and result['finished_at'] >= released_at[0], result
