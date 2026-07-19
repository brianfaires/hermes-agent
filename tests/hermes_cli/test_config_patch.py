"""Regression tests for structural ``hermes config patch`` mutations."""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from hermes_cli import config
from hermes_cli.config import apply_config_patch


@pytest.fixture(autouse=True)
def hermes_home(tmp_path):
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        yield tmp_path


def read_config(home):
    return yaml.safe_load((home / "config.yaml").read_text())


def test_config_write_lock_uses_windows_byte_range_lock(hermes_home):
    calls = []

    def locking(fd, mode, length):
        calls.append((mode, length, os.lseek(fd, 0, os.SEEK_CUR)))

    fake_msvcrt = SimpleNamespace(LK_LOCK=1, LK_UNLCK=2, locking=locking)
    config_path = hermes_home / "config.yaml"

    with patch.dict(sys.modules, {"fcntl": None, "msvcrt": fake_msvcrt}):
        lock = config._config_write_lock(config_path)
        assert lock is not None
        config._release_config_write_lock(lock)

    assert calls == [(fake_msvcrt.LK_LOCK, 1, 0), (fake_msvcrt.LK_UNLCK, 1, 0)]
    assert (hermes_home / ".config.yaml.lock").stat().st_size >= 1


def test_patch_add_replace_remove_map_list_and_scalar(hermes_home):
    (hermes_home / "config.yaml").write_text(
        "model:\n  aliases:\n    stable: openai/gpt-4\n"
        "fallback_providers:\n- provider: alpha\nenabled: false\n"
    )

    apply_config_patch("add", "/model/aliases/fast", '"openai/gpt-5"')
    apply_config_patch("add", "/fallback_providers/-", '{"provider": "beta"}')
    apply_config_patch("replace", "/enabled", "true")
    apply_config_patch("remove", "/fallback_providers/0")

    assert read_config(hermes_home) == {
        "model": {"aliases": {"stable": "openai/gpt-4", "fast": "openai/gpt-5"}},
        "fallback_providers": [{"provider": "beta"}],
        "enabled": True,
    }


def test_patch_json_pointer_escapes_map_keys(hermes_home):
    (hermes_home / "config.yaml").write_text("model:\n  aliases: {}\n")

    apply_config_patch("add", "/model/aliases/release~11~0fast", '"provider/model"')

    assert read_config(hermes_home)["model"]["aliases"] == {"release/1~fast": "provider/model"}


@pytest.mark.parametrize("value", ["not-json", "NaN", "Infinity", "-Infinity"])
def test_patch_rejects_non_json_values_without_writing(hermes_home, value):
    config_path = hermes_home / "config.yaml"
    original = "model:\n  aliases: {}\n"
    config_path.write_text(original)

    with pytest.raises(ValueError):
        apply_config_patch("add", "/model/aliases/fast", value)

    assert config_path.read_text() == original


def test_patch_rejects_invalid_or_missing_paths_without_writing(hermes_home):
    config_path = hermes_home / "config.yaml"
    original = "model:\n  aliases: {}\nfallback_providers: []\n"
    config_path.write_text(original)

    with pytest.raises(ValueError):
        apply_config_patch("add", "model.aliases.fast", '"openai/gpt-5"')
    with pytest.raises(ValueError):
        apply_config_patch("remove", "/model/aliases/missing")
    with pytest.raises(ValueError):
        apply_config_patch("replace", "/fallback_providers/-", "{}")

    assert config_path.read_text() == original


def test_patch_rejects_managed_leaf_and_ancestor(hermes_home, tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("model:\n  default: managed/model\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    from hermes_cli import managed_scope
    managed_scope.invalidate_managed_cache()

    for path in ("/model/default", "/model"):
        with pytest.raises(PermissionError):
            apply_config_patch("replace", path, '"user/override"')

    managed_scope.invalidate_managed_cache()


def test_patch_rejects_fully_managed_installation(hermes_home, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
    monkeypatch.setattr("hermes_cli.config.managed_error", lambda _action: None)

    with pytest.raises(PermissionError, match="managed"):
        apply_config_patch("add", "/model/default", '"provider/model"')

    assert not (hermes_home / "config.yaml").exists()
