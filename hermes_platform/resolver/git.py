"""Recovery binaries belonging to the selected Git-for-Windows installation."""
from pathlib import Path


def git_core_candidates() -> list[Path]:
    """Inspect the selected tool's layout without executing its potentially broken shim."""
    from hermes_platform.resolver import locate_command

    command = locate_command("git").command
    roots = []
    if command:
        executable = Path(command[0])
        if executable.parent.name.casefold() in {"cmd", "bin"}:
            roots.append(executable.parent.parent)
    roots.extend(Path(base) / "Git" for base in (r"C:\Program Files", r"C:\Program Files (x86)"))
    return [root / architecture / "libexec" / "git-core" / "git.exe"
            for root in roots
            for architecture in ("clangarm64", "mingw64", "mingw32")]
