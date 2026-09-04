"""disk_cleanup — ephemeral file cleanup for Hermes Agent.

Library module wrapping the deterministic cleanup rules written by
@LVT382009 in PR #12212. The plugin ``__init__.py`` wires these
functions into ``post_tool_call`` and ``on_session_end`` hooks so
tracking and cleanup happen automatically — the agent never needs to
call a tool or remember a skill.

Rules:
  - test files    → delete immediately at task end (age >= 0)
  - temp files    → delete after 7 days
  - cron-output   → delete after 14 days
  - empty dirs    → always delete (under HERMES_HOME)
  - research      → keep 10 newest, prompt for older (deep only)
  - chrome-profile→ prompt after 14 days (deep only)
  - >500 MB files → prompt always (deep only)

Scope: strictly HERMES_HOME and /tmp/hermes-*
Never touches: ~/.hermes/logs/ or any system directory.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover — plugin may load before constants resolves
    import os

    def get_hermes_home() -> Path:  # type: ignore[no-redef]
        val = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(val).resolve() if val else (Path.home() / ".hermes").resolve()


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_WILDCARD_SYNTAX_CHARS = frozenset("*?[")

def get_state_dir() -> Path:
    """State dir — separate from ``$HERMES_HOME/logs/``."""
    return get_hermes_home() / "disk-cleanup"


def get_tracked_file() -> Path:
    return get_state_dir() / "tracked.json"


def get_log_file() -> Path:
    """Audit log — intentionally NOT under ``$HERMES_HOME/logs/``."""
    return get_state_dir() / "cleanup.log"


def _has_wildcard_syntax(path_str: str) -> bool:
    return any(char in path_str for char in _WILDCARD_SYNTAX_CHARS)


def _is_wildcard_policy(path_str: str) -> bool:
    parent_text = path_str[:-2]
    return (
        path_str.endswith("/*")
        and not _has_wildcard_syntax(parent_text)
        and Path(parent_text).is_absolute()
    )


def _wildcard_parent(path_str: str) -> Optional[Path]:
    if not _is_wildcard_policy(path_str):
        return None
    return Path(path_str[:-2])


def _is_safe_wildcard_policy_parent(parent: Path) -> bool:
    """Return True when *parent* is safe to use as a recursive policy root."""
    if not parent.is_dir() or parent.is_symlink():
        return False
    if not is_safe_path(parent):
        return False

    hermes_home = get_hermes_home()
    try:
        rel = parent.resolve().relative_to(hermes_home)
    except (ValueError, OSError):
        return True

    if not rel.parts:
        return False

    top = rel.parts[0]
    if top in {"cron", "cronjobs"}:
        return rel.parts == ("cron", "output")
    if top in _EMPTY_DIR_PROTECTED_TOP_LEVEL:
        return False
    if _is_durable_script_path(parent):
        return False
    return True


def _retention_days(category: str) -> Optional[int]:
    if category == "test":
        return 0
    if category == "temp":
        return 7
    if category == "cron-output":
        return 14
    return None


def _wildcard_policy_parent(item: Dict[str, Any]) -> Optional[Path]:
    parent = _wildcard_parent(str(item.get("path", "")))
    if parent is None:
        return None
    if _retention_days(str(item.get("category", ""))) is None:
        return None
    if not _is_safe_wildcard_policy_parent(parent):
        return None
    try:
        return parent.resolve()
    except OSError:
        return None


def _valid_wildcard_policy_roots(tracked: Iterable[Dict[str, Any]]) -> set[Path]:
    roots: set[Path] = set()
    for item in tracked:
        parent = _wildcard_policy_parent(item)
        if parent is not None:
            roots.add(parent)
    return roots


def _is_at_or_below_valid_wildcard_root(path: Path, roots: set[Path]) -> bool:
    if not roots:
        return False
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _iter_safe_wildcard_files(
    parent: Path,
    category: str,
    now: datetime,
) -> Iterable[Dict[str, Any]]:
    threshold = _retention_days(category)
    if threshold is None:
        return

    for child in parent.rglob("*"):
        if child.is_symlink() or not child.is_file():
            continue
        try:
            resolved = child.resolve()
            resolved.relative_to(parent)
        except (ValueError, OSError):
            continue
        if (
            not is_safe_path(resolved)
            or _is_durable_script_path(resolved)
            or _is_protected_cron_path(resolved)
        ):
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        age = (now - mtime).days
        if category == "test" or age > threshold:
            yield {
                "path": str(resolved),
                "timestamp": mtime.isoformat(),
                "category": category,
                "size": st.st_size,
            }


def _remove_empty_wildcard_dirs(parent: Path) -> int:
    removed = 0
    dirs = [
        p for p in parent.rglob("*")
        if p.is_dir() and not p.is_symlink() and not _is_durable_script_path(p)
    ]
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            d.resolve().relative_to(parent)
            if not any(d.iterdir()):
                d.rmdir()
                removed += 1
                _log(f"DELETED: {d} (empty dir via wildcard)")
        except (ValueError, OSError):
            continue
    return removed


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def is_safe_path(path: Path) -> bool:
    """Accept only paths under HERMES_HOME or ``/tmp/hermes-*``.

    Rejects Windows mounts (``/mnt/c`` etc.) and any system directory.
    """
    hermes_home = get_hermes_home()
    try:
        path.resolve().relative_to(hermes_home)
        return True
    except (ValueError, OSError):
        pass
    # Allow /tmp/hermes-* explicitly
    parts = path.parts
    if len(parts) >= 3 and parts[1] == "tmp" and parts[2].startswith("hermes-"):
        return True
    return False


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _log(message: str) -> None:
    try:
        log_file = get_log_file()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except OSError:
        # Never let the audit log break the agent loop.
        pass


# ---------------------------------------------------------------------------
# tracked.json — atomic read/write, backup scoped to tracked.json only
# ---------------------------------------------------------------------------

def load_tracked() -> List[Dict[str, Any]]:
    """Load tracked.json.  Restores from ``.bak`` on corruption."""
    tf = get_tracked_file()
    tf.parent.mkdir(parents=True, exist_ok=True)

    if not tf.exists():
        return []

    try:
        return json.loads(tf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        bak = tf.with_suffix(".json.bak")
        if bak.exists():
            try:
                data = json.loads(bak.read_text(encoding="utf-8"))
                _log("WARN: tracked.json corrupted — restored from .bak")
                return data
            except Exception:
                pass
        _log("WARN: tracked.json corrupted, no backup — starting fresh")
        return []


def save_tracked(tracked: List[Dict[str, Any]]) -> None:
    """Atomic write: ``.tmp`` → backup old → rename."""
    tf = get_tracked_file()
    tf.parent.mkdir(parents=True, exist_ok=True)
    tmp = tf.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tracked, indent=2), encoding="utf-8")
    if tf.exists():
        shutil.copy2(tf, tf.with_suffix(".json.bak"))
    tmp.replace(tf)


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

ALLOWED_CATEGORIES = {
    "temp", "test", "research", "download",
    "chrome-profile", "cron-output", "other",
}

_EMPTY_DIR_PROTECTED_TOP_LEVEL = frozenset({
    "logs", "memories", "sessions", "cron", "cronjobs",
    "cache", "scripts", "skills", "plugins", "disk-cleanup", "optional-skills",
    "hermes-agent", "backups", "profiles", ".worktrees",
    # User-authored project trees — never sweep empty directories
    # inside these (#75403).
    "patches", "projects", "skins", "themes", "contributors",
})

_EMPTY_DIR_SWEEP_PRUNE_DIRS = frozenset({
    ".git", "node_modules", "venv", ".venv",
    "site-packages", "__pycache__",
})


# Paths under $HERMES_HOME that must NEVER be deleted by quick(),
# regardless of what the stored category says.  This is a defense-in-depth
# guard against stale tracked.json entries from before #34840.
_PROTECTED_CRON_PATHS: set[str] = set()


def _is_protected_cron_path(p: Path) -> bool:
    """Return True if *p* is a cron control-plane file/directory that must
    never be deleted.

    This matches, by EXACT path only, the ``cron/`` directory itself, known
    control-plane files (``jobs.json``, ``.tick.lock``), and the ``output/``
    root directory. It does NOT (and must not be "simplified" to) blanket-match
    everything under ``cron/output/`` — those run artifacts are disposable and
    are cleaned by retention policy; only the ``output/`` root itself is
    protected, because deleting it wholesale erases every job's retained run
    history at once.
    """
    # Lazily build the set once per process so HERMES_HOME is resolved
    # exactly once.
    if not _PROTECTED_CRON_PATHS:
        hermes_home = get_hermes_home()
        for parent in ("cron", "cronjobs"):
            base = hermes_home / parent
            _PROTECTED_CRON_PATHS.add(str(base))
            _PROTECTED_CRON_PATHS.add(str(base / "output"))
            _PROTECTED_CRON_PATHS.add(str(base / "jobs.json"))
            _PROTECTED_CRON_PATHS.add(str(base / ".tick.lock"))
    resolved = str(p.resolve())
    return resolved in _PROTECTED_CRON_PATHS


def _is_durable_script_path(p: Path) -> bool:
    """Return True for user-maintained scripts under active or named profiles."""
    hermes_home = get_hermes_home()
    try:
        rel = p.resolve().relative_to(hermes_home)
    except (ValueError, OSError):
        return False

    parts = rel.parts
    return (
        len(parts) >= 1
        and parts[0] == "scripts"
    ) or (
        len(parts) >= 3
        and parts[0] == "profiles"
        and parts[2] == "scripts"
    )


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ---------------------------------------------------------------------------
# Track / forget
# ---------------------------------------------------------------------------

def track(path_str: str, category: str, silent: bool = False) -> bool:
    """Register a file for tracking. Returns True if newly tracked."""
    if category not in ALLOWED_CATEGORIES:
        _log(f"WARN: unknown category '{category}', using 'other'")
        category = "other"

    wildcard_parent = _wildcard_parent(path_str)
    if wildcard_parent is not None:
        policy_parent = _wildcard_policy_parent({
            "path": path_str,
            "category": category,
        })
        if policy_parent is None:
            _log(f"REJECT: {path_str} (unsafe wildcard policy)")
            return False
        path = policy_parent
        stored_path = f"{policy_parent}/*"
    elif _has_wildcard_syntax(path_str):
        _log(f"REJECT: {path_str} (unsupported wildcard policy)")
        return False
    else:
        path = Path(path_str).resolve()
        stored_path = str(path)

    if not path.exists():
        _log(f"SKIP: {stored_path} (does not exist)")
        return False

    if not is_safe_path(path):
        _log(f"REJECT: {stored_path} (outside HERMES_HOME)")
        return False

    size = (
        path.stat().st_size
        if path.is_file() and wildcard_parent is None
        else 0
    )
    tracked = load_tracked()

    # Deduplicate
    if any(item["path"] == stored_path for item in tracked):
        return False

    tracked.append({
        "path": stored_path,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "size": size,
    })
    save_tracked(tracked)
    _log(f"TRACKED: {stored_path} ({category}, {fmt_size(size)})")
    if not silent:
        print(f"Tracked: {stored_path} ({category}, {fmt_size(size)})")
    return True


def forget(path_str: str) -> int:
    """Remove a path from tracking without deleting the file."""
    wildcard_parent = _wildcard_parent(path_str)
    if wildcard_parent is not None:
        stored_path = f"{wildcard_parent}/*"
    elif _has_wildcard_syntax(path_str):
        stored_path = path_str
    else:
        p = Path(path_str).resolve()
        stored_path = str(p)
    tracked = load_tracked()
    before = len(tracked)
    tracked = [i for i in tracked if i["path"] != stored_path]
    removed = before - len(tracked)
    if removed:
        save_tracked(tracked)
        _log(f"FORGOT: {stored_path} ({removed} entries)")
    return removed


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def dry_run() -> Tuple[List[Dict], List[Dict]]:
    """Return (auto_delete_list, needs_prompt_list) without touching files."""
    tracked = load_tracked()
    now = datetime.now(timezone.utc)
    wildcard_roots = _valid_wildcard_policy_roots(tracked)

    auto: List[Dict] = []
    prompt: List[Dict] = []

    for item in tracked:
        path_str = str(item.get("path", ""))
        wildcard_parent = _wildcard_policy_parent(item)
        if wildcard_parent is not None:
            auto.extend(
                _iter_safe_wildcard_files(
                    wildcard_parent, item["category"], now
                )
            )
            continue
        if _has_wildcard_syntax(path_str):
            continue

        p = Path(path_str)
        if not p.exists():
            continue
        if _is_at_or_below_valid_wildcard_root(p, wildcard_roots):
            continue
        if _is_durable_script_path(p):
            continue
        age = (now - datetime.fromisoformat(item["timestamp"])).days
        cat = item["category"]
        size = item["size"]

        # Re-validate stale "cron-output" entries (fixes #37721).
        if cat == "cron-output":
            re_cat = guess_category(p)
            if re_cat != "cron-output":
                # Stale entry — would be skipped by quick(); omit from
                # dry-run output too.
                continue

        if cat == "test":
            auto.append(item)
        elif cat == "temp" and age > 7:
            auto.append(item)
        elif cat == "cron-output" and age > 14:
            auto.append(item)
        elif cat == "research" and age > 30:
            prompt.append(item)
        elif cat == "chrome-profile" and age > 14:
            prompt.append(item)
        elif size > 500 * 1024 * 1024:
            prompt.append(item)

    return auto, prompt


# ---------------------------------------------------------------------------
# Quick cleanup
# ---------------------------------------------------------------------------

def quick() -> Dict[str, Any]:
    """Safe deterministic cleanup — no prompts.

    Returns: ``{"deleted": N, "empty_dirs": N, "freed": bytes,
               "errors": [str, ...]}``.
    """
    tracked = load_tracked()
    now = datetime.now(timezone.utc)
    deleted = 0
    freed = 0
    new_tracked: List[Dict] = []
    errors: List[str] = []
    wildcard_sweep_roots = _valid_wildcard_policy_roots(tracked)
    empty_removed = 0

    for item in tracked:
        path_str = str(item.get("path", ""))
        wildcard_parent = _wildcard_policy_parent(item)
        if wildcard_parent is not None:
            for file_item in _iter_safe_wildcard_files(
                wildcard_parent, item["category"], now
            ):
                p = Path(file_item["path"])
                try:
                    p.unlink()
                    freed += file_item["size"]
                    deleted += 1
                    _log(
                        f"DELETED: {p} ({item['category']}, "
                        f"{fmt_size(file_item['size'])}) via wildcard "
                        f"{item['path']}"
                    )
                except OSError as e:
                    _log(f"ERROR deleting {p}: {e}")
                    errors.append(f"{p}: {e}")
            empty_removed += _remove_empty_wildcard_dirs(wildcard_parent)
            new_tracked.append(item)
            continue

        if _has_wildcard_syntax(path_str):
            _log(f"DROP invalid wildcard policy: {path_str}")
            continue

        p = Path(path_str)
        cat = item["category"]

        if not p.exists():
            _log(f"STALE: {p} (removed from tracking)")
            continue

        if _is_at_or_below_valid_wildcard_root(p, wildcard_sweep_roots):
            _log(f"SKIP wildcard-managed path: {p} (removed from tracking)")
            continue

        if _is_durable_script_path(p):
            _log(f"SKIP durable script path: {p} (removed from tracking)")
            continue

        age = (now - datetime.fromisoformat(item["timestamp"])).days

        # ---- stale-state migration (fixes #37721) ----
        # Old tracked.json entries may carry a "cron-output" category for
        # paths that are NOT under cron/output/ (e.g. cron/jobs.json).
        # guess_category() was fixed in #34840, but existing entries are
        # never re-validated.  Re-classify here so stale entries for cron
        # control-plane state are not deleted.
        if cat == "cron-output":
            re_cat = guess_category(p)
            if re_cat != "cron-output":
                _log(
                    f"SKIP stale cron-output entry: {p} "
                    f"(re-classified as {re_cat!r})"
                )
                # Drop the stale entry — it was misclassified.
                continue

        # ---- stale-state migration for 'test' category (fixes #75403) ----
        # Old tracked.json entries may carry a "test" category for paths
        # that are now under protected project directories (patches/,
        # projects/, etc.).  guess_category() was tightened in the fix for
        # #75403, but existing entries are never re-validated.  Re-classify
        # here so stale entries for protected paths are not deleted.
        if cat == "test":
            re_cat = guess_category(p)
            if re_cat != "test":
                _log(
                    f"SKIP stale test entry: {p} "
                    f"(re-classified as {re_cat!r} — under protected tree)"
                )
                continue

        # Hard safety net: never delete cron control-plane state even if
        # the category somehow slipped through re-validation above.
        if _is_protected_cron_path(p):
            _log(f"SKIP protected cron path: {p}")
            continue

        should_delete = (
            cat == "test"
            or (cat == "temp" and age > 7)
            or (cat == "cron-output" and age > 14)
        )

        if should_delete:
            try:
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    shutil.rmtree(p)
                freed += item["size"]
                deleted += 1
                _log(f"DELETED: {p} ({cat}, {fmt_size(item['size'])})")
            except OSError as e:
                _log(f"ERROR deleting {p}: {e}")
                errors.append(f"{p}: {e}")
                new_tracked.append(item)
        else:
            new_tracked.append(item)

    # Remove empty dirs under HERMES_HOME, but never recurse into known
    # durable state trees.  Some installs place the Hermes checkout, venv,
    # and desktop build under HERMES_HOME; a full rglob over that tree can
    # stall the gateway event loop for minutes.
    hermes_home = get_hermes_home()
    sweep_stack: List[Tuple[Path, bool]] = []
    try:
        for top in hermes_home.iterdir():
            if (
                top.is_dir()
                and not top.is_symlink()
                and top.name not in _EMPTY_DIR_PROTECTED_TOP_LEVEL
                and top.name not in _EMPTY_DIR_SWEEP_PRUNE_DIRS
            ):
                sweep_stack.append((top, False))
    except OSError:
        sweep_stack = []

    while sweep_stack:
        dirpath, visited = sweep_stack.pop()
        if visited:
            try:
                if (
                    dirpath not in wildcard_sweep_roots
                    and not any(dirpath.iterdir())
                ):
                    dirpath.rmdir()
                    empty_removed += 1
                    _log(f"DELETED: {dirpath} (empty dir)")
            except OSError:
                pass
            continue

        sweep_stack.append((dirpath, True))
        try:
            for child in dirpath.iterdir():
                if (
                    child.is_dir()
                    and not child.is_symlink()
                    and child.name not in _EMPTY_DIR_SWEEP_PRUNE_DIRS
                ):
                    sweep_stack.append((child, False))
        except OSError:
            pass

    save_tracked(new_tracked)
    _log(
        f"QUICK_SUMMARY: {deleted} files, {empty_removed} dirs, "
        f"{fmt_size(freed)}"
    )
    return {
        "deleted": deleted,
        "empty_dirs": empty_removed,
        "freed": freed,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Deep cleanup (interactive — not called from plugin hooks)
# ---------------------------------------------------------------------------

def deep(
    confirm: Optional[callable] = None,
) -> Dict[str, Any]:
    """Deep cleanup.

    Runs :func:`quick` first, then asks the *confirm* callable for each
    risky item (research > 30d beyond 10 newest, chrome-profile > 14d,
    any file > 500 MB).  *confirm(item)* must return True to delete.

    Returns: ``{"quick": {...}, "deep_deleted": N, "deep_freed": bytes}``.
    """
    quick_result = quick()

    if confirm is None:
        # No interactive confirmer — deep stops after the quick pass.
        return {"quick": quick_result, "deep_deleted": 0, "deep_freed": 0}

    tracked = load_tracked()
    now = datetime.now(timezone.utc)
    research, chrome, large = [], [], []

    for item in tracked:
        p = Path(item["path"])
        if not p.exists():
            continue
        age = (now - datetime.fromisoformat(item["timestamp"])).days
        cat = item["category"]

        if cat == "research" and age > 30:
            research.append(item)
        elif cat == "chrome-profile" and age > 14:
            chrome.append(item)
        elif item["size"] > 500 * 1024 * 1024:
            large.append(item)

    research.sort(key=lambda x: x["timestamp"], reverse=True)
    old_research = research[10:]

    freed, count = 0, 0
    to_remove: List[Dict] = []

    for group in (old_research, chrome, large):
        for item in group:
            if confirm(item):
                try:
                    p = Path(item["path"])
                    if p.is_file():
                        p.unlink()
                    elif p.is_dir():
                        shutil.rmtree(p)
                    to_remove.append(item)
                    freed += item["size"]
                    count += 1
                    _log(
                        f"DELETED: {p} ({item['category']}, "
                        f"{fmt_size(item['size'])})"
                    )
                except OSError as e:
                    _log(f"ERROR deleting {item['path']}: {e}")

    if to_remove:
        remove_paths = {i["path"] for i in to_remove}
        save_tracked([i for i in tracked if i["path"] not in remove_paths])

    return {"quick": quick_result, "deep_deleted": count, "deep_freed": freed}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def status() -> Dict[str, Any]:
    """Return per-category breakdown and top 10 largest tracked files."""
    tracked = load_tracked()
    cats: Dict[str, Dict] = {}
    for item in tracked:
        c = item["category"]
        cats.setdefault(c, {"count": 0, "size": 0})
        cats[c]["count"] += 1
        cats[c]["size"] += item["size"]

    existing = [
        (i["path"], i["size"], i["category"])
        for i in tracked
        if (
            _wildcard_policy_parent(i) is not None
            or (
                not _has_wildcard_syntax(str(i.get("path", "")))
                and Path(i["path"]).exists()
            )
        )
    ]
    existing.sort(key=lambda x: x[1], reverse=True)

    return {
        "categories": cats,
        "top10": existing[:10],
        "total_tracked": len(tracked),
    }


def format_status(s: Dict[str, Any]) -> str:
    """Human-readable status string (for slash command output)."""
    lines = [f"{'Category':<20} {'Files':>6}  {'Size':>10}", "-" * 40]
    cats = s["categories"]
    for cat, d in sorted(cats.items(), key=lambda x: x[1]["size"], reverse=True):
        lines.append(f"{cat:<20} {d['count']:>6}  {fmt_size(d['size']):>10}")

    if not cats:
        lines.append("(nothing tracked yet)")

    lines.append("")
    lines.append("Top 10 largest tracked files:")
    if not s["top10"]:
        lines.append("  (none)")
    else:
        for rank, (path, size, cat) in enumerate(s["top10"], 1):
            lines.append(f"  {rank:>2}. {fmt_size(size):>8}  [{cat}]  {path}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auto-categorisation from tool-call inspection
# ---------------------------------------------------------------------------

_TEST_PATTERNS = ("test_", "tmp_")
_TEST_SUFFIXES = (".test.py", ".test.js", ".test.ts", ".test.md")


def guess_category(path: Path) -> Optional[str]:
    """Return a category label for *path*, or None if we shouldn't track it.

    Used by the ``post_tool_call`` hook to auto-track ephemeral files.
    """
    if not is_safe_path(path):
        return None

    # Skip the state dir itself, logs, memory files, sessions, config.
    hermes_home = get_hermes_home()
    try:
        rel = path.resolve().relative_to(hermes_home)
        top = rel.parts[0] if rel.parts else ""
        if top in {
            "disk-cleanup", "logs", "memories", "sessions", "config.yaml",
            "scripts", "skills", "plugins", ".env", "USER.md", "MEMORY.md",
            "SOUL.md", "auth.json", "hermes-agent",
            # User-authored and project trees — never auto-delete files
            # inside these just because they happen to be named test_* or
            # tmp_* (#75403, also #32164, #37721).
            "patches", "projects", "skins", "themes", "contributors",
            "profiles", "backups", "optional-skills",
        }:
            return None
        if top == "cron" or top == "cronjobs":
            # Only files under the disposable ``output/`` subtree are
            # cleanup candidates. Top-level cron control-plane state
            # (e.g. ``jobs.json``, ``.tick.lock``) must never be
            # auto-tracked — deleting it wipes the live scheduler
            # registry. See issue #32164.
            if len(rel.parts) >= 3 and rel.parts[1] == "output":
                return "cron-output"
            return None
        if top == "cache":
            return "temp"
    except ValueError:
        # Path isn't under HERMES_HOME (e.g. /tmp/hermes-*) — fall through.
        pass

    name = path.name
    if name.startswith(_TEST_PATTERNS):
        return "test"
    if any(name.endswith(sfx) for sfx in _TEST_SUFFIXES):
        return "test"
    return None
