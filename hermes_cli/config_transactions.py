"""Shared config read-modify-write exclusion for native and external mutations."""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Optional

_HOLDERS: dict[str, threading.local] = {}
_HOLDERS_GUARD = threading.Lock()


@contextmanager
def config_mutation_lock(config_path: str | Path):
    """Lock a directory entry across atomic replacements, reentrantly per thread.

    Writers take the file lock before the reader/cache lock, so a competing
    process cannot stall local readers while this writer waits. Callers must
    keep the complete read-modify-write inside this context.
    """
    from hermes_cli import auth, config
    from hermes_constants import mkdir_under_hermes_home

    path = Path(config_path).expanduser()
    path = path.parent.resolve() / path.name
    if auth.fcntl is None and auth.msvcrt is None:
        raise RuntimeError("Cross-process config locking is unavailable on this host")
    mkdir_under_hermes_home(path.parent)
    with _HOLDERS_GUARD:
        holder = _HOLDERS.setdefault(str(path), threading.local())
    with auth._file_lock(path.with_name('.' + path.name + '.lock'), holder, 15.0,
                         f"Timed out waiting for configuration lock ({path})"):
        with config._CONFIG_LOCK:
            yield


def config_path_mutation(function):
    """Decorate a path-first YAML mutation whose read and write must share the lock."""
    @wraps(function)
    def protected(path, *args, **kwargs):
        with config_mutation_lock(path):
            return function(path, *args, **kwargs)
    return protected


def mutate_config(config_path: str | Path, mutation):
    """Apply one callback to fresh raw YAML and persist once through the native writer."""
    from hermes_cli import config
    path = Path(config_path)
    with config_mutation_lock(path):
        state = config.require_readable_config_before_write(path)
        before = copy.deepcopy(state)
        result = mutation(state)
        config.atomic_config_replace(path, state)
        from hermes_cli.config_side_effects import sync_config_changes
        sync_config_changes(path, before, state)
        return result


def _omitted_config_paths(
    existing: dict[str, Any], proposed: dict[str, Any], prefix: tuple[str, ...] = (),
) -> list[str]:
    """Mapping paths that *proposed* would delete by omission from *existing*.

    The round-trip writer recurses through mappings, so the completeness check must recurse too:
    checking top-level key counts still lets a partial ``plugins: {...}`` payload erase sibling
    settings inside that section. Replacing a non-empty mapping with a scalar/list is likewise a
    deletion of that mapping's children and is reported at the mapping path.
    """
    omitted: list[str] = []
    for key, old_value in existing.items():
        path = (*prefix, str(key))
        if key not in proposed:
            omitted.append(".".join(path))
            continue
        new_value = proposed[key]
        if isinstance(old_value, dict):
            if isinstance(new_value, dict):
                omitted.extend(_omitted_config_paths(old_value, new_value, path))
            elif old_value:
                omitted.append(".".join(path))
    return omitted


@config_path_mutation
def write_config_state(
    config_path: Path, data: dict[str, Any], *, allow_omissions: bool,
    extra_content_on_create: Optional[str] = None,
) -> None:
    """Shared comment-preserving config writer; omission policy is selected by the public wrapper."""
    from utils import atomic_roundtrip_yaml_save
    from hermes_cli.config_snapshots import check_config_snapshot, refresh_config_snapshot
    from hermes_cli.config import _refuse_failed_read, require_readable_config_before_write, _refuse_overwrite

    check_config_snapshot(config_path, data)
    _refuse_failed_read(config_path, data)
    if not allow_omissions:
        existing = require_readable_config_before_write(config_path)
        omitted = _omitted_config_paths(existing, data)
        if omitted:
            shown = ", ".join(omitted[:12])
            if len(omitted) > 12:
                shown += f", +{len(omitted) - 12} more"
            exc = ValueError(f"omitted config paths: {shown}")
            raise _refuse_overwrite(
                config_path,
                "would lose settings omitted by this write",
                exc,
                "Pass the complete current config, or use atomic_config_replace() only when "
                "deletion by omission is deliberate.",
            ) from exc
    atomic_roundtrip_yaml_save(config_path, data, extra_content_on_create=extra_content_on_create)
    refresh_config_snapshot(config_path, data)
