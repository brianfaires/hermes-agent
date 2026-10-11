"""Live-system guard: refuse production Hermes paths from a test process.

Hermetic test isolation rides on the process environment: ``HERMES_HOME``
says *where* to write and ``PYTEST_*`` / ``HERMES_TEST_ISOLATION`` say
*whether this is a test*.  Both live in the same carrier, so anything that
drops the environment — a child spawned with a rebuilt env, or an in-process
``patch.dict(os.environ, {}, clear=True)`` — loses the redirect and the
"this is a test" signal in one step.  Code that then falls back to the
platform default home writes into the operator's live ``~/.hermes``.

This module is the shared answer to "is this a test process, and is that
path production?".  It is deliberately stdlib-only (``psutil`` is imported
lazily) so lightweight modules such as ``gateway.status`` can use it without
pulling in ``hermes_state``.  ``hermes_state`` re-exports the detection names
for backward compatibility.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

#: Escape hatch for a test that genuinely needs the live system.  The in-tree
#: conftest sets this for tests marked ``@pytest.mark.live_system_guard_bypass``.
BYPASS = False

#: Env-carried twin of ``BYPASS``, for spawned children (a module global
#: cannot cross a process boundary).  Shared with the state.db guard.
BYPASS_ENV = "HERMES_STATE_DB_GUARD_BYPASS"

#: Additional production roots to refuse beyond the platform default.  The
#: test conftest injects a pre-sandbox custom ``HERMES_HOME`` here.
EXTRA_DENY_ROOTS: Tuple[Path, ...] = ()

#: Env marker exported by the hermetic test conftest at the same moment it
#: redirects ``HERMES_HOME`` to the per-session tmp isolation root.  Unlike
#: ``PYTEST_*`` (owned by pytest, and routinely scrubbed by tests that rebuild
#: a child environment), this marker is OURS and inherits into children by
#: default — a process that resolves a production path while carrying it is,
#: by definition, an isolation escape (#82770).
TEST_ISOLATION_MARKER_ENV = "HERMES_TEST_ISOLATION"

#: Names that identify a pytest launcher in a process command line.  Matched
#: against the *basename* of each argv token so ``/tmp/pytest-of-dev/...``
#: paths — which do show up in real argv — cannot false-positive.
_PYTEST_LAUNCHER_NAMES = frozenset({"pytest", "py.test", "pytest.exe", "py.test.exe"})

#: Memoised process-tree answer.  The tree above (and including) us does not
#: change in a way that matters here, and the walk must not cost anything on
#: the hot path.
_PYTEST_IN_PROCESS_TREE: Optional[bool] = None


def real_platform_root() -> Optional[Path]:
    """Resolve the REAL platform-default Hermes root.

    Deliberately avoids ``Path.home()`` / ``hermes_constants``: tests
    routinely monkeypatch ``Path.home`` to a tempdir, and resolving through
    the patched callable would misidentify the test's own hermetic home as
    "production" (false positive) or miss the real one (false negative).
    ``os.path.expanduser`` reads the HOME environment variable / passwd
    entry, which the hermetic conftest never rewrites.
    """
    try:
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA", "").strip()
            root = (
                Path(base) / "hermes"
                if base
                else Path(os.path.expanduser("~")) / "AppData" / "Local" / "hermes"
            )
        else:
            root = Path(os.path.expanduser("~")) / ".hermes"
        return root.resolve()
    except Exception:
        return None


def _passwd_root() -> Optional[Path]:
    """The Hermes root under the account's passwd home (POSIX only).

    With ``HOME`` removed from the environment, ``Path.home()`` falls back to
    the passwd entry — so this is where env-clearing tests actually land,
    even if ``HOME`` was pointed elsewhere when the guard resolved its roots.
    """
    if sys.platform == "win32":
        return None
    try:
        import pwd

        return (Path(pwd.getpwuid(os.getuid()).pw_dir) / ".hermes").resolve()
    except Exception:
        return None


def production_roots() -> List[Path]:
    roots: List[Path] = []
    for root in (real_platform_root(), _passwd_root()):
        if root is not None and root not in roots:
            roots.append(root)
    for extra in EXTRA_DENY_ROOTS:
        try:
            roots.append(Path(extra).expanduser().resolve())
        except Exception:
            continue
    return roots


def running_under_pytest() -> bool:
    """True when the environment says this process is a pytest run."""
    return bool(
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("PYTEST_VERSION")
        or os.environ.get(TEST_ISOLATION_MARKER_ENV)
    )


def process_looks_like_pytest(proc: Any) -> bool:
    """True when *proc*'s command line is a pytest invocation.

    Covers both ``pytest ...`` (launcher on argv[0]) and ``python -m pytest``
    (launcher as a bare ``pytest`` token).  A process whose command line we
    cannot read is treated as "not pytest": guessing the other way would
    refuse production opens for unrelated reasons.
    """
    try:
        cmdline = proc.cmdline() or []
    except Exception:
        return False
    for arg in cmdline:
        try:
            token = str(arg).strip('"').strip("'")
            # Split on both separators on every host: os.path.basename is
            # POSIX-only under Linux and would leave a Windows-style path
            # intact, making the matcher's answer depend on the platform.
            name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
        except Exception:
            continue
        if name in _PYTEST_LAUNCHER_NAMES:
            return True
    return False


def pytest_in_process_tree() -> bool:
    """True when this process or one of its ancestors is a pytest run.

    Process identity is the one test-context signal that survives an env
    wipe.  The current process is included: an in-process
    ``patch.dict(os.environ, {}, clear=True)`` scrubs ``PYTEST_*`` and the
    isolation marker from the pytest process itself, whose parent (the
    per-file test runner) is not a pytest launcher.

    Fails open (``False``) when ``psutil`` is unavailable or the walk errors —
    that restores env-only behaviour rather than blocking real user runs on a
    psutil hiccup.
    """
    global _PYTEST_IN_PROCESS_TREE
    if _PYTEST_IN_PROCESS_TREE is not None:
        return _PYTEST_IN_PROCESS_TREE
    found = False
    try:
        import psutil

        me = psutil.Process()
        found = any(process_looks_like_pytest(p) for p in (me, *me.parents()))
    except Exception:
        found = False
    _PYTEST_IN_PROCESS_TREE = found
    return found


def in_test_context() -> bool:
    """True when this process is a test run, by environment or process tree.

    The env probe is a few dict lookups and covers the common case, so the
    process walk only runs for processes the environment claims are ordinary
    user runs — and its answer is memoised, so a real ``hermes`` invocation
    pays for at most one walk.
    """
    return running_under_pytest() or pytest_in_process_tree()


def is_production_home(path: Path) -> bool:
    """True when *path* is a real Hermes root or a profile home directly under one."""
    try:
        resolved = Path(path).expanduser().resolve()
    except Exception:
        return False
    for root in production_roots():
        if resolved == root:
            return True
        if resolved.parent.name == "profiles" and resolved.parent.parent == root:
            return True
    return False


def ensure_not_production_home(home: Path, what: str) -> None:
    """Raise when a test-context process resolves *home* to a live Hermes home.

    No-op outside tests, for hermetic (tmp) homes, and when bypassed.
    """
    if BYPASS or os.environ.get(BYPASS_ENV):
        return
    if not in_test_context():
        return
    if is_production_home(home):
        raise RuntimeError(
            f"live-system guard: test resolved {what} under the production "
            f"Hermes home {Path(home).expanduser()}. The test environment lost "
            "its HERMES_HOME redirect (e.g. patch.dict(os.environ, {}, "
            "clear=True) drops HOME and HERMES_HOME). Keep HERMES_HOME in the "
            "patched env, or mark the test @pytest.mark.live_system_guard_bypass."
        )
