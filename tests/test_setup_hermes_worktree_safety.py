"""Regression coverage for worktree-safe setup-hermes.sh behavior."""

import os
import shutil
import subprocess
from pathlib import Path


def test_setup_script_refuses_a_git_worktree_without_touching_global_launcher(tmp_path):
    source = Path(__file__).parents[1] / "setup-hermes.sh"
    project = tmp_path / "worktree"
    project.mkdir()
    script = project / "setup-hermes.sh"
    shutil.copy2(source, script)
    script.chmod(0o755)
    (project / ".git").write_text("gitdir: /tmp/fake-worktree\n", encoding="utf-8")

    home = tmp_path / "home"
    launcher_dir = home / ".local" / "bin"
    launcher_dir.mkdir(parents=True)
    existing_target = tmp_path / "canonical-hermes"
    existing_target.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher = launcher_dir / "hermes"
    launcher.symlink_to(existing_target)

    env = os.environ | {"HOME": str(home), "HERMES_HOME": str(home / ".hermes")}
    result = subprocess.run(
        ["bash", str(script)],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert "Git worktree" in result.stdout
    assert launcher.resolve() == existing_target
