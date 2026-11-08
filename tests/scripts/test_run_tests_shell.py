"""The canonical shell preserves test inputs, strips credentials and propagates failure."""
from pathlib import Path
import os
import subprocess
import sys

import pytest


@pytest.mark.platforms("posix")
def test_shell_runner_executes_tests_and_propagates_failure(tmp_path):
    case = tmp_path / "test_runner_canary.py"
    marker = tmp_path / "executed"
    case.write_text(
        "from pathlib import Path\nimport os\n"
        "from tests.e2e.core.upgrade.git import _git_world as G\n"
        "def test_canary():\n"
        f"    Path({str(marker)!r}).write_text('executed')\n"
        "    assert os.environ['PATHEXT'] == '.COM;.EXE;.BAT;.CMD'\n"
        "    assert os.environ['HERMES_E2E_UPGRADE_BASE'] == 'fixture-upgrade-base'\n"
        "    assert G.n1_tag() == 'fixture-upgrade-base'\n"
        "    assert 'OPENAI_API_KEY' not in os.environ\n"
        "    assert False, 'runner failure propagation canary'\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), "-j", "1", str(case)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180,
        env={**os.environ, "HERMES_PYTHON": sys.executable, "HERMES_TEST_FILE_RETRIES": "0",
             "HERMES_E2E_UPGRADE_BASE": "fixture-upgrade-base", "OPENAI_API_KEY": "synthetic-canary",
             "PATHEXT": ".COM;.EXE;.BAT;.CMD"},
             check=False,
    )
    assert marker.read_text(encoding="utf-8") == "executed", result.stdout + result.stderr
    assert result.returncode != 0, result.stdout + result.stderr
    assert "runner failure propagation canary" in result.stdout + result.stderr, result.stdout + result.stderr
