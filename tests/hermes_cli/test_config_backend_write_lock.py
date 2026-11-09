"""Two writers setting different keys through the public config write path both survive."""
import threading

import pytest

import hermes_yaml as yaml
import utils
from hermes_cli.config_backend import write_config_key


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("race: {}\n")
    return path


def test_concurrent_writers_keep_both_settings(config_path, monkeypatch):
    original = utils._roundtrip_load
    first_loaded, second_loaded, release_first = (threading.Event() for _ in range(3))
    errors: list[BaseException] = []

    def load_then_pause(path):
        loaded = original(path)
        if threading.current_thread().name == "first":
            first_loaded.set()
            assert release_first.wait(10), "test failed to release first writer"
        else:
            second_loaded.set()
        return loaded

    def write(name):
        try:
            write_config_key(config_path, f"race.{name}", 1)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(utils, "_roundtrip_load", load_then_pause)
    first = threading.Thread(target=write, args=("first",), name="first")
    second = threading.Thread(target=write, args=("second",), name="second")
    first.start()
    assert first_loaded.wait(10)
    second.start()
    # With no mutation lock the second writer loads and commits inside the first's window.
    second_loaded.wait(2)
    release_first.set()
    first.join(10)
    second.join(10)

    assert not errors and not first.is_alive() and not second.is_alive()
    assert yaml.safe_load(config_path.read_text())["race"] == {"first": 1, "second": 1}
