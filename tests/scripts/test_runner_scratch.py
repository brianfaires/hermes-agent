"""Runner scratch selection and the canonical shell's infrastructure allowlist."""
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from scripts import run_tests_parallel as runner


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("override", [None, ""])
def test_default_scratch_uses_var_tmp(monkeypatch, override):
    monkeypatch.delenv("HERMES_TEST_SCRATCH_ROOT", raising=False)
    if override is not None:
        monkeypatch.setenv("HERMES_TEST_SCRATCH_ROOT", override)
    monkeypatch.setenv("TMPDIR", "/unused-temp")
    monkeypatch.setattr(runner.os.path, "isdir", lambda path: path == "/var/tmp")
    mkdir = Mock()
    monkeypatch.setattr(runner.os, "makedirs", mkdir)

    root = runner._runner_scratch_root()

    assert root == f"/var/tmp/hermes-pytest-{os.getuid()}"
    mkdir.assert_called_once_with(root, exist_ok=True)


@pytest.mark.parametrize("override", [None, ""])
def test_default_scratch_falls_back_to_tempdir(tmp_path, monkeypatch, override):
    monkeypatch.delenv("HERMES_TEST_SCRATCH_ROOT", raising=False)
    if override is not None:
        monkeypatch.setenv("HERMES_TEST_SCRATCH_ROOT", override)
    monkeypatch.setattr(runner.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))
    mkdir = Mock()
    monkeypatch.setattr(runner.os, "makedirs", mkdir)

    root = Path(runner._runner_scratch_root())

    assert root.parent == tmp_path
    assert root.name == "hermes-pytest" + (f"-{os.getuid()}" if hasattr(os, "getuid") else "")
    mkdir.assert_called_once_with(str(root), exist_ok=True)


def test_scratch_override_selects_supplied_base(tmp_path, monkeypatch):
    base = tmp_path / "scratch base"
    monkeypatch.setenv("HERMES_TEST_SCRATCH_ROOT", str(base))
    mkdir = Mock()
    monkeypatch.setattr(runner.os, "makedirs", mkdir)

    root = Path(runner._runner_scratch_root())

    assert root.parent == base
    mkdir.assert_called_once_with(str(root), exist_ok=True)


def test_override_creates_child_and_sweep_preserves_base_siblings(tmp_path, monkeypatch):
    base = tmp_path / "scratch base"
    monkeypatch.setenv("HERMES_TEST_SCRATCH_ROOT", str(base))
    root = Path(runner._runner_scratch_root())
    assert root.parent == base
    assert root.name == "hermes-pytest" + (f"-{os.getuid()}" if hasattr(os, "getuid") else "")
    assert root.is_dir()
    sentinel = base / "unrelated"
    sentinel.write_text("keep", encoding="utf-8")
    stale = root / "r-abandoned"
    stale.mkdir()
    old = time.time() - 48 * 3600
    os.utime(stale, (old, old))
    os.utime(sentinel, (old, old))

    runner._sweep_killed_run_roots(str(root))

    assert not stale.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert root.is_dir()


@pytest.mark.platforms("posix")
def test_shell_forwards_scratch_base_to_real_test(tmp_path):
    base = tmp_path / "nested scratch"
    home = tmp_path / "home"
    home.mkdir()
    temp = tmp_path / "tmp"
    temp.mkdir()
    case = tmp_path / "test_scratch_canary.py"
    marker = tmp_path / "executed"
    case.write_text(
        "import os\nfrom pathlib import Path\n"
        "def test_scratch_canary(tmp_path):\n"
        f"    base = Path({str(base)!r})\n"
        "    assert os.environ['HERMES_TEST_SCRATCH_ROOT'] == str(base)\n"
        "    assert Path(os.environ['TMPDIR']).is_relative_to(base)\n"
        "    assert tmp_path.is_relative_to(base)\n"
        "    assert 'OPENAI_API_KEY' not in os.environ\n"
        f"    Path({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["bash", str(repo / "scripts/run_tests.sh"), "-j2", str(case)],
        cwd=repo, capture_output=True, text=True, timeout=180,
        env={"PATH": os.environ["PATH"], "HOME": str(home),
             "HERMES_HOME": str(home / ".hermes"), "TMPDIR": str(temp),
             "HERMES_PYTHON": sys.executable, "HERMES_TEST_SCRATCH_ROOT": str(base),
             "PYTHONPYCACHEPREFIX": str(tmp_path / "bytecode"),
             "OPENAI_API_KEY": "synthetic-canary"},
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text(encoding="utf-8") == "executed"
