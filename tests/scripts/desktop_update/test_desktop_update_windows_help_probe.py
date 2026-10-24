"""The bounded help probe has its own process/file deadline on native Windows.

Keeping this potentially 120-second case separate lets the outcome cases finish
and report their assertions within the runner's five-minute per-file limit.
"""
from pathlib import Path

import pytest

from tests.scripts.desktop_update.test_desktop_update_windows_commit_outcome import _handoff


@pytest.mark.platforms('windows')
def test_hanging_update_help_probe_is_bounded(tmp_path: Path) -> None:
    code, out, argv, _result, _ = _handoff(
        tmp_path, '-NoGateway', '-ProbeTimeoutSeconds', '10', timeout=120, HANDOFF_HELP_HANG='1',
    )
    assert code == 0, out
    assert argv[0][:2] == ['update', '--yes'] and '--keep-stash' not in argv[0], argv
