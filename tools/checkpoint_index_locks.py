"""Explicit, conservative recovery of one checkpoint project's abandoned Git index lock."""
from __future__ import annotations

import os
from pathlib import Path
import stat
from uuid import uuid4

from tools.checkpoint_pruning import PruneError, store_lock


def recover_index_lock(working_dir: str, *, apply: bool = False) -> dict:
    """Inspect under the store lock; quarantine only after two negative writer checks."""
    from hermes_cli._early_recovery import _held_open
    from tools import checkpoint_manager as cm

    base = cm._resolve_checkpoint_base()
    store = cm._store_path(base)
    project = cm._project_hash(working_dir)
    lock = cm._index_path(store, project).with_suffix(".lock")
    with store_lock(base):
        try:
            before = lock.lstat()
        except FileNotFoundError:
            return {"status": "absent", "lock": str(lock)}
        if not stat.S_ISREG(before.st_mode):
            raise PruneError("Refusing recovery: index lock is not a regular file")
        if _held_open(lock, store, any_git=True) is not False:
            raise PruneError("Refusing recovery: index lock has an active or uninspectable owner")
        if not apply:
            return {"status": "stale", "lock": str(lock)}

        quarantine = base / "quarantined-index-locks"
        quarantine.mkdir(exist_ok=True)
        destination = quarantine / f"{project}-{uuid4().hex}.lock"
        if _held_open(lock, store, any_git=True) is not False:
            raise PruneError("Refusing recovery: a writer appeared during inspection")
        after = lock.lstat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise PruneError("Refusing recovery: index lock changed during inspection")
        os.rename(lock, destination)
        return {"status": "quarantined", "lock": str(lock), "quarantine": str(destination)}


def cmd_recover_index_lock(args) -> int:
    try:
        result = recover_index_lock(args.working_dir, apply=args.apply)
    except (OSError, PruneError) as exc:
        print(f"Checkpoint index lock kept: {exc}")
        print("Let Git/checkpoint writers finish and fix inspection/access errors, "
              "then rerun without --apply.")
        return 2
    if result["status"] == "stale":
        print(f"No writer found for {result['lock']}; rerun with --apply to quarantine it.")
    elif result["status"] == "quarantined":
        print(f"Quarantined {result['lock']} to {result['quarantine']}")
    else:
        print(f"No index lock at {result['lock']}")
    return 0
