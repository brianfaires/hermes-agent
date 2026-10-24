"""Git recovery follows the selected managed tool, including native ARM layouts."""
from pathlib import Path

import pytest

from hermes_cli import update_cmd, update_cmd_git
from hermes_platform.resolver import Candidate, Resolution


@pytest.mark.parametrize("architecture", ["mingw64", "clangarm64"])
def test_git_recovery_finds_core_beside_selected_managed_tool(tmp_path, monkeypatch, architecture):
    root = tmp_path / "managed-git"
    shim = root / "cmd" / "git.exe"
    shim.parent.mkdir(parents=True)
    shim.touch()
    core = root / architecture / "libexec" / "git-core" / "git.exe"
    core.parent.mkdir(parents=True)
    core.touch()
    monkeypatch.setattr(
        "hermes_platform.resolver.locate_command",
        lambda name: Resolution("path_executable", (Candidate(str(shim), "PATH", True),)),
    )
    monkeypatch.setattr(update_cmd_git, "_probe_fork_bomb", lambda argv: Path(argv[0]) != core)

    assert update_cmd._locate_real_git() == core
