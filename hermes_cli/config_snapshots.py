"""Optimistic conflict detection for configuration snapshots edited outside a lock."""
from __future__ import annotations

from functools import wraps
from pathlib import Path

from ruamel.yaml.representer import SafeRepresenter


class ConfigConflictError(RuntimeError):
    """The file changed after a caller read the state it is trying to save."""


class ConfigSnapshot(dict):
    """A normal mapping with non-serialized source identity for write-back checks."""

    def update(self, other=(), **kwargs):
        # Setup refreshes its shared mapping in place after nested writers save.
        # Only a complete replacement can adopt the incoming source revision.
        replace = not self and isinstance(other, ConfigSnapshot) and not kwargs
        super().update(other, **kwargs)
        if replace:
            self.source_path = other.source_path
            self.source_revision = other.source_revision

    def copy(self):
        result = type(self)(self)
        result.source_path = self.source_path
        result.source_revision = self.source_revision
        return result


# Revision metadata belongs to the process, never to serialized configuration.
SafeRepresenter.add_representer(ConfigSnapshot, SafeRepresenter.represent_dict)


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


def read_config_snapshot(path, reader):
    """Version a mutable read without locking the runtime's read-only hot path."""
    from hermes_cli import config
    # Frozen workers read their owner-supplied snapshot and rebase edits at save.
    # That state has no live-file revision and must not probe the profile file.
    if path is None and config._worker_config_snapshot() is not None:
        return reader()
    path = _canonical(config.get_config_path() if path is None else path)
    for _ in range(2):
        before = _revision(path)
        state = reader()
        after = _revision(path)
        if isinstance(state, config.FailedConfigRead):
            return state
        if before == after:
            break
    snapshot = ConfigSnapshot(state)
    snapshot.source_path = path
    # Runtime readers must stay available under churn; an uncertain mutable
    # snapshot can be read, but cannot overwrite a newer configuration.
    snapshot.source_revision = after if before == after else ("unstable",)
    return snapshot


def config_snapshot_reader(function):
    """Attach source identity to raw mutable loader results."""
    @wraps(function)
    def read(*args, **kwargs):
        path = args[0] if args else kwargs.get('config_path')
        return read_config_snapshot(path, lambda: function(*args, **kwargs))
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
