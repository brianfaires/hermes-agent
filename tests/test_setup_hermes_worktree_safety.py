"""Regression coverage for worktree-safe setup-hermes.sh behavior."""

import os
import shutil
import subprocess
from pathlib import Path


def test_setup_script_skips_global_launcher_in_git_worktree(tmp_path):
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

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        """#!/bin/bash
set -e
case "$1 $2" in
  "--version ") echo "uv 0.test" ;;
  "python find") command -v python3 ;;
  "python install") exit 0 ;;
  "venv venv")
    mkdir -p venv/bin
    ln -s "$(command -v python3)" venv/bin/python
    printf '#!/bin/sh\\n' > venv/bin/hermes
    chmod +x venv/bin/hermes
    ;;
  "sync --extra"|"pip install") exit 0 ;;
  *) exit 0 ;;
esac
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    env = os.environ | {
        "HOME": str(home),
        "HERMES_HOME": str(home / ".hermes"),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    result = subprocess.run(
        ["bash", str(script)],
        env=env,
        input="n\nn\n",
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Git worktree detected" in result.stdout
    assert "Skipping global hermes launcher" in result.stdout
    assert "Setup complete" in result.stdout
    assert (project / "venv" / "bin" / "hermes").exists()
    assert launcher.resolve() == existing_target
