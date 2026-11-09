"""update_config_document is an atomic cross-process read-modify-write on the config backend."""
import multiprocessing
import threading
import time

import pytest

import hermes_yaml as yaml
from hermes_cli.config_backend import update_config_document


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("# keep me\nrace: {}\n")
    return path


def _add_key_slowly(home, key, ready, delay):
    import os
    os.environ["HERMES_HOME"] = home

    def mutator(doc):
        ready.set()
        time.sleep(delay)  # health: allow HX013 -- the held lock across a slow mutator is the behaviour under test; no code event marks "peer is blocked on the flock"
        doc["race"][key] = 1

    update_config_document(mutator)


def test_two_processes_updating_different_keys_both_survive(config_path):
    ctx = multiprocessing.get_context("spawn")
    ready_a, ready_b = ctx.Event(), ctx.Event()
    home = str(config_path.parent)
    a = ctx.Process(target=_add_key_slowly, args=(home, "a", ready_a, 1.0))
    b = ctx.Process(target=_add_key_slowly, args=(home, "b", ready_b, 0.0))
    a.start()
    assert ready_a.wait(30)  # a is inside its mutator, holding the lock
    b.start()
    a.join(30)
    b.join(30)

    assert a.exitcode == 0 and b.exitcode == 0
    assert yaml.safe_load(config_path.read_text())["race"] == {"a": 1, "b": 1}


def test_mutator_raising_leaves_file_unchanged(config_path):
    before = config_path.read_bytes()

    def boom(doc):
        doc["race"]["x"] = 1
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError, match="abort"):
        update_config_document(boom)
    assert config_path.read_bytes() == before


def test_returns_written_document_and_accepts_replacement(config_path):
    written = update_config_document(lambda doc: {"fresh": 1})
    assert written == {"fresh": 1}
    assert yaml.safe_load(config_path.read_text()) == {"fresh": 1}


def test_absent_file_starts_from_empty_document(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    update_config_document(lambda doc: doc.update(a=1))
    assert yaml.safe_load((tmp_path / "config.yaml").read_text()) == {"a": 1}


def test_update_inside_a_plugin_state_lock_does_not_deadlock(config_path):
    from hermes_cli.plugins_state import _locked_plugin_state

    done = threading.Event()

    def nested():
        with _locked_plugin_state(config_path):
            update_config_document(lambda doc: doc["race"].update(nested=1))
        done.set()

    threading.Thread(target=nested, daemon=True).start()
    assert done.wait(10), "update_config_document deadlocked against the plugin state lock"
    assert yaml.safe_load(config_path.read_text())["race"] == {"nested": 1}
