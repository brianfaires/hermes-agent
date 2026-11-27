"""Git-core discovery reads PM layouts without installing or changing the host OS."""

import subprocess
from types import SimpleNamespace

import pytest

import pm
from pm import paths
from pm.lock import Lockfile
from hermes_cli import update_cmd, update_cmd_git, update_custody


@pytest.mark.parametrize("arch", ["clangarm64", "mingw64", "mingw32"])
@pytest.mark.parametrize("recorded", [False, True])
def test_locator_probes_real_binary_in_recorded_or_staged_pm_entry(tmp_path, monkeypatch, arch, recorded):
    store = tmp_path / "tools"
    target = "win32-arm64" if arch == "clangarm64" else "win32-x64"
    version = Lockfile(paths.lockfile_path()).version("git")
    entry = store / pm.get_package("git").store_entry(version, target)
    if recorded:
        entry = store / "previous-selected-git"
    binary = entry / arch / "libexec" / "git-core" / "git.exe"
    binary.parent.mkdir(parents=True)
    binary.touch()
    shim = entry / "cmd" / "git.exe"
    shim.parent.mkdir()
    shim.touch()
    monkeypatch.setattr(paths, "store_root", lambda: store)
    monkeypatch.setattr(pm, "current_target", lambda: target)  # package target data, not interpreter OS
    monkeypatch.setattr(pm, "installed_package", lambda name, **kw:
                        SimpleNamespace(path=entry, binary=shim) if recorded else None)

    def probe(argv, args, **kwargs):
        # Any installed Program Files copy is intentionally unusable in this layout.
        return subprocess.CompletedProcess(argv, 0 if argv == [str(binary)] else 1,
                                           "git version fixture" if argv == [str(binary)] else "", "")

    monkeypatch.setattr(update_custody, "run_git", probe)
    assert update_cmd._locate_real_git() == binary

    # Existing-but-broken candidates must not suppress the caller's ZIP fallback.
    monkeypatch.setattr(update_custody, "run_git", lambda argv, args, **kw:
                        subprocess.CompletedProcess(argv, 1, "", "BUG (fork bomb)"))
    assert update_cmd._locate_real_git() is None


def test_unsuccessful_version_probe_is_not_a_healthy_git(monkeypatch):
    monkeypatch.setattr(update_custody, "run_git", lambda argv, args, **kw:
                        subprocess.CompletedProcess(argv, 1, "", "cannot load DLL"))
    assert update_cmd_git._probe_fork_bomb(["unusable-git"]) is None
