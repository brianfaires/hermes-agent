"""Optimistic conflict detection for configuration snapshots edited outside a lock."""
from __future__ import annotations

from functools import wraps
from pathlib import Path


class ConfigConflictError(RuntimeError):
    """The file changed after a caller read the state it is trying to save."""


class ConfigSnapshot(dict):
    """A normal mapping with non-serialized source identity for write-back checks."""

    def copy(self):
        result = type(self)(self)
        result.source_path = self.source_path
        result.source_revision = self.source_revision
        return result


def _canonical(path):
    path = Path(path).expanduser()
    return path.parent.resolve() / path.name


def _revision(path):
    from utils import file_signature
    try:
        return file_signature(path.stat())
    except FileNotFoundError:
        return None
    except OSError:
        # Let the native loader preserve its unreadable-file/fallback behavior.
        return ("unreadable",)


def config_snapshot_reader(function):
    """Version mutable loader results without locking the runtime's read-only hot path."""
    @wraps(function)
    def read(*args, **kwargs):
        from hermes_cli import config
        path = args[0] if args else kwargs.get('config_path')
        path = _canonical(path if path is not None else config.get_config_path())
        for _ in range(2):
            before = _revision(path)
            state = function(*args, **kwargs)
            after = _revision(path)
            if isinstance(state, config.FailedConfigRead):
                return state
            if before == after:
                snapshot = ConfigSnapshot(state)
                snapshot.source_path = path
                snapshot.source_revision = after
                return snapshot
        raise ConfigConflictError(f'Configuration changed while reading {path}; reload and retry')
    return read


def check_config_snapshot(path, state):
    """Called under the shared writer lock, before normalization discards metadata."""
    if isinstance(state, ConfigSnapshot):
        if state.source_path != _canonical(path) or state.source_revision != _revision(_canonical(path)):
            raise ConfigConflictError(f'Configuration changed since it was read ({path}); reload and retry')


def refresh_config_snapshot(path, state):
    """Allow a caller to edit and save its same snapshot again after a successful save."""
    if isinstance(state, ConfigSnapshot):
        state.source_revision = _revision(_canonical(path))


def config_save_transaction(function):
    """Protect legacy load/edit/save callers with a lock and optimistic version check."""
    @wraps(function)
    def save(state, *args, **kwargs):
        from hermes_cli import config
        from hermes_cli.config_transactions import config_mutation_lock
        if config.is_managed():
            return function(state, *args, **kwargs)
        path = config.get_config_path()
        with config_mutation_lock(path):
            check_config_snapshot(path, state)
            result = function(state, *args, **kwargs)
            refresh_config_snapshot(path, state)
            return result
    return save
