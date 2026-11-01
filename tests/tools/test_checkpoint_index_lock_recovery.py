"""Targeted quarantine preserves checkpoint history and live writers (#177)."""
import argparse
from pathlib import Path

import pytest

from hermes_cli.checkpoints import register_cli
from tools import checkpoint_manager as cm


def _recover(workdir, *, apply=False):
    parser = argparse.ArgumentParser()
    register_cli(parser)
    args = parser.parse_args(["recover-index-lock", str(workdir), *(["--apply"] if apply else [])])
    return args.func(args)


@pytest.fixture
def checkpoint(tmp_path, monkeypatch):
    base = tmp_path / "checkpoints"
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", base)
    workdir = tmp_path / "project"
    workdir.mkdir()
    file = workdir / "hello.txt"
    file.write_text("before\n")
    manager = cm.CheckpointManager(enabled=True, max_snapshots=50)
    assert manager.ensure_checkpoint(str(workdir))
    store = cm._store_path()
    index = cm._index_path(store, cm._project_hash(str(workdir)))
    lock = index.with_name(index.name + ".lock")
    lock.touch()
    return manager, workdir, file, store, index, lock


@pytest.mark.platforms("linux")
def test_quarantine_recovers_checkpoint_without_changing_existing_history(checkpoint):
    manager, workdir, file, store, index, lock = checkpoint
    sibling = workdir.parent / "other-project"
    sibling.mkdir()
    (sibling / "other.txt").write_text("other history\n")
    assert manager.ensure_checkpoint(str(sibling))
    sibling_index = cm._index_path(store, cm._project_hash(str(sibling)))
    sibling_lock = sibling_index.with_name(sibling_index.name + ".lock")
    sibling_lock.touch()
    original = manager.list_checkpoints(str(workdir))
    index_bytes = index.read_bytes()
    file.write_text("after\n")
    manager.new_turn()
    assert not manager.ensure_checkpoint(str(workdir))  # exact incident symptom
    assert _recover(workdir) == 0  # preview keeps everything in place
    assert lock.exists()
    assert _recover(workdir, apply=True) == 0
    assert not lock.exists() and sibling_lock.exists()
    quarantined, = (store.parent / "quarantined-index-locks").iterdir()
    assert quarantined.read_bytes() == b""
    assert index.read_bytes() == index_bytes
    assert manager.list_checkpoints(str(workdir)) == original
    manager.new_turn()
    assert manager.ensure_checkpoint(str(workdir))
    assert len(manager.list_checkpoints(str(workdir))) == len(original) + 1


@pytest.mark.parametrize("condition", ["open_writer", "unknown_owner", "busy_store", "appearing_writer"])
def test_active_unknown_or_busy_store_keeps_index_lock(checkpoint, monkeypatch, condition):
    from contextlib import ExitStack
    from hermes_cli import _early_recovery
    from tools.checkpoint_pruning import store_lock

    _, workdir, _, store, index, lock = checkpoint
    before = index.read_bytes()
    with ExitStack() as stack:
        if condition == "open_writer":
            stack.enter_context(lock.open("rb"))
        elif condition == "unknown_owner":
            monkeypatch.setattr(_early_recovery, "_held_open", lambda *_a, **_kw: None)
        elif condition == "busy_store":
            stack.enter_context(store_lock(store.parent))
        else:
            probe = _early_recovery._held_open
            checks = []

            def writer_appears(*args, **kwargs):
                checks.append(True)
                if len(checks) == 2:
                    stack.enter_context(lock.open("rb"))
                return probe(*args, **kwargs)

            monkeypatch.setattr(_early_recovery, "_held_open", writer_appears)
        assert _recover(workdir, apply=True) == 2
    assert lock.exists() and index.read_bytes() == before
    quarantine = store.parent / "quarantined-index-locks"
    assert not quarantine.exists() or not list(quarantine.iterdir())
