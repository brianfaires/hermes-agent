"""Strict raw-reader and real registered cleanup regression probes."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

from hermes_cli.config import read_user_config_raw


NON_MAPPING_ROOTS = ["[]", "false", "0", '""', "[one]", "42", "literal"]


def snapshot(root):
    return {
        str(path.relative_to(root)): (
            path.read_bytes(), path.stat().st_mtime_ns
        ) if path.is_file() else None
        for path in root.rglob("*")
    }


@pytest.mark.parametrize("raw", NON_MAPPING_ROOTS)
def test_strict_rejects_nonmapping_without_changing_default(tmp_path, raw):
    path = tmp_path / "config.yaml"
    path.write_text(raw, encoding="utf-8")
    before = snapshot(tmp_path)
    assert read_user_config_raw(path) == {}
    assert read_user_config_raw(path, strict_mapping=False) == {}
    with pytest.raises(ValueError):
        read_user_config_raw(path, strict_mapping=True)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("raw", [None, "", "# empty\n", "null", "~", "{}"])
def test_missing_null_and_mapping_remain_valid(tmp_path, raw):
    path = tmp_path / "config.yaml"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    before = snapshot(tmp_path)
    assert read_user_config_raw(path) == {}
    assert read_user_config_raw(path, strict_mapping=True) == {}
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("strict", [False, True])
def test_reader_is_literal_uncached_and_read_only(tmp_path, monkeypatch, strict):
    monkeypatch.setenv("STRICT_READER_PROBE", "expanded")
    path = tmp_path / "config.yaml"
    path.write_text("probe: '${STRICT_READER_PROBE}'\n", encoding="utf-8")
    before = snapshot(tmp_path)
    assert read_user_config_raw(path, strict_mapping=strict) == {
        "probe": "${STRICT_READER_PROBE}"
    }
    assert snapshot(tmp_path) == before
    path.write_text("probe: changed\n", encoding="utf-8")
    before = snapshot(tmp_path)
    assert read_user_config_raw(path, strict_mapping=strict) == {
        "probe": "changed"
    }
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("failure", ["yaml", "io"])
def test_reader_propagates_errors_without_mutation(tmp_path, strict, failure):
    path = tmp_path / "config.yaml"
    if failure == "yaml":
        path.write_text("[invalid", encoding="utf-8")
        error = yaml.YAMLError
    else:
        path.mkdir()
        error = OSError
    before = snapshot(tmp_path)
    with pytest.raises(error):
        read_user_config_raw(path, strict_mapping=strict)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("raw", NON_MAPPING_ROOTS + ["[invalid"])
def test_registered_cleanup_keeps_sentinel_on_malformed_root(
    tmp_path, monkeypatch, raw
):
    from hermes_constants import (
        reset_hermes_home_override, set_hermes_home_override,
    )

    home = tmp_path / "owner"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    owner_token = set_hermes_home_override(home)
    try:
        source = Path(__file__).resolve().parents[2] / "plugins/disk-cleanup"
        name = "disk_cleanup_strict_config_probe"
        spec = importlib.util.spec_from_file_location(
            name, source / "__init__.py",
            submodule_search_locations=[str(source)],
        )
        plugin = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, plugin)
        spec.loader.exec_module(plugin)

        class Context:
            def __init__(self):
                self.hooks = {}

            def register_hook(self, name, handler):
                self.hooks[name] = handler

            def register_command(self, name, handler, description):
                self.handler = handler

        (home / "config.yaml").write_text("{}", encoding="utf-8")
        ctx = Context()
        plugin.register(ctx)
        sentinel = home / "test_kept.py"
        sentinel.write_text("keep", encoding="utf-8")
        entry = {"path": str(sentinel), "category": "test", "size": 4,
                 "timestamp": "2020-01-01T00:00:00+00:00"}
        plugin.dg.save_tracked([entry])
        (home / "config.yaml").write_text(raw, encoding="utf-8")
        other = tmp_path / "other"
        other.mkdir()
        (other / "config.yaml").write_text("{}", encoding="utf-8")
        foreign_token = set_hermes_home_override(other)
        try:
            before = snapshot(tmp_path)
            status = json.loads(ctx.handler("protection " + str(sentinel)))
            assert status["home"] == str(home)
            assert status["policy_valid"] is False
            assert snapshot(tmp_path) == before
            plugin._recent_test_tracks["strict-root"] = {str(sentinel)}
            ctx.hooks["on_session_end"](session_id="strict-root")
            assert sentinel.read_text(encoding="utf-8") == "keep"
        finally:
            reset_hermes_home_override(foreign_token)
    finally:
        reset_hermes_home_override(owner_token)
