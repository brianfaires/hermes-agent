"""Live Windows E2E for the Git-for-Windows trampoline self-heal (#87876).

Runs ONLY on a real windows-latest runner (wine2e/** on-demand lane). No
mocked subprocess anywhere: the probes drive the REAL
``_git_is_trampoline`` / ``_locate_real_git`` / ``_ensure_non_trampoline_git``
helpers against the runner's genuine Git-for-Windows install plus a real
broken-trampoline stand-in (a .bat that reproduces the launcher's
"BUG (fork bomb)" guard output and refuses to run).

The runner supplies Git for Windows either in Program Files or in setup-pm's
recorded tool store. The fixture binds that existing store after test-home
isolation so the swap path exercises the real locator end to end.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("windows")  # live Windows trampoline E2E

_FORK_BOMB_BAT = (
    "@echo off\r\n"
    "echo BUG (fork bomb): tried to spawn itself, check your PATH 1>&2\r\n"
    "exit /b 1\r\n"
)


def _make_broken_trampoline(tmp_path: Path) -> Path:
    bat = tmp_path / "git.bat"
    bat.write_text(_FORK_BOMB_BAT, encoding="utf-8")
    return bat


@pytest.fixture(autouse=True)
def runner_git_store(monkeypatch):
    # The canonical runner drops HERMES_RUNTIME_DIR. Borrow only the recorded
    # setup store for this read-only live probe; keep HERMES_HOME disposable.
    result = subprocess.run(["git", "--exec-path"], capture_output=True, text=True,
                            timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    core = Path(result.stdout.strip())
    assert core.is_absolute() and core.name == "git-core" and core.parent.name == "libexec", core
    store = core.parents[3]
    if (store / "facts.json").is_file():
        import pm

        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(store))
        recorded = pm.installed_package("git", allow_outdated=True)
        assert recorded is not None and recorded.path.resolve() == core.parents[2].resolve(), core


def _runner_git_diagnostics() -> str:
    """Read the runner's actual layout and probe failures; never provision a Git."""
    from hermes_cli.update_cmd_git import _portable_git_candidates

    result = subprocess.run(["git", "--exec-path"], capture_output=True, text=True,
                            timeout=30, check=False)
    details = [f"git --exec-path: rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"]
    candidates = _portable_git_candidates()
    if result.returncode == 0 and Path(result.stdout.strip()).is_absolute():
        core = Path(result.stdout.strip())
        candidates = [core / "git.exe", core.parent.parent / "bin" / "git.exe", *candidates]
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file():
            details.append(f"{candidate}: absent")
            continue
        try:
            probe = subprocess.run([str(candidate), "--version"], capture_output=True,
                                   text=True, timeout=15, check=False)
            details.append(f"{candidate}: rc={probe.returncode} stdout={probe.stdout[-500:]!r} "
                           f"stderr={probe.stderr[-500:]!r}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            details.append(f"{candidate}: {exc}")
    return "\n".join(details)


class TestGitTrampolineLive:
    def test_runner_git_is_not_a_trampoline(self):
        from hermes_cli import update_cmd

        # Healthy PATH git on the runner: probe must report False and the
        # probe itself must actually run git (sanity: git exists here).
        version = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=30,
            check=False,
        )
        assert version.returncode == 0, version.stderr
        assert update_cmd._git_is_trampoline(["git"]) is False

    def test_broken_trampoline_detected_live(self, tmp_path):
        from hermes_cli import update_cmd

        bat = _make_broken_trampoline(tmp_path)
        assert update_cmd._git_is_trampoline([str(bat)]) is True

    def test_locate_real_git_finds_runner_git_core(self):
        from hermes_cli import update_cmd

        real = update_cmd._locate_real_git()
        # Both standard and PM installs supply a runnable git-core binary.
        assert real is not None, _runner_git_diagnostics()
        assert real.exists()
        assert real.name == "git.exe"
        probe = subprocess.run(
            [str(real), "--version"], capture_output=True, text=True, timeout=30,
            check=False,
        )
        assert probe.returncode == 0
        assert "git version" in probe.stdout.lower()

    def test_ensure_non_trampoline_git_swaps_live(self, tmp_path, capsys):
        from hermes_cli import update_cmd

        bat = _make_broken_trampoline(tmp_path)
        broken_cmd = [str(bat), "-c", "windows.appendAtomically=false"]
        healed = update_cmd._ensure_non_trampoline_git(broken_cmd)

        assert healed != broken_cmd, _runner_git_diagnostics()
        assert healed[1:] == broken_cmd[1:], "git config args must survive"
        real = Path(healed[0])
        assert real.exists() and real.name == "git.exe"
        # The healed command must actually work.
        result = subprocess.run(
            healed + ["--version"], capture_output=True, text=True, timeout=30,
            check=False,
        )
        assert result.returncode == 0
        assert "switching to real git" in capsys.readouterr().out

    def test_healthy_git_command_untouched_live(self):
        from hermes_cli import update_cmd

        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
        assert update_cmd._ensure_non_trampoline_git(git_cmd) == git_cmd
