"""Exact exemption contract; all destructive probes use disposable homes."""
import importlib.util
import json
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def home(tmp_path, monkeypatch):
    p = tmp_path / "home"
    p.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(p))
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(p)
    yield p
    reset_hermes_home_override(token)


@pytest.fixture
def dg(home):
    source = Path(__file__).resolve().parents[2] / "plugins/disk-cleanup/disk_cleanup.py"
    spec = importlib.util.spec_from_file_location("exemption_lib", source)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def configure(home, paths):
    config = {"plugins": {"entries": {"disk-cleanup": {"settings": {"exempt_paths": paths}}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config))


def record(path, category="test", size=1):
    return {"path": str(path), "category": category, "size": size,
            "timestamp": "2020-01-01T00:00:00+00:00"}


def test_quick_preserves_exempt_stale_record_and_empty_descendants(home, dg):
    root = home / "tests"
    empty = root / "empty"
    empty.mkdir(parents=True)
    file = root / "test_durable.py"
    file.write_text("durable")
    ordinary = home / "test_disposable.py"
    ordinary.write_text("temp")
    dg.save_tracked([record(file), record(ordinary)])
    configure(home, [str(root)])
    result = dg.quick()
    assert file.read_text() == "durable"
    assert empty.is_dir()
    assert not ordinary.exists()
    assert result["deleted"] == 1
    assert dg.guess_category(file) is None
    assert not dg.track(str(file), "temp", silent=True)


def test_wildcard_exemption_keeps_empty_tree_but_cleans_sibling(home, dg):
    parent = home / "scratch"
    protected = parent / "tests"
    (protected / "empty").mkdir(parents=True)
    durable = protected / "test_kept.py"
    durable.write_text("keep")
    other = parent / "other.py"
    other.write_text("delete")
    dg.save_tracked([record(str(parent) + "/*")])
    configure(home, [str(protected)])
    auto, prompt = dg.dry_run()
    assert [item["path"] for item in auto] == [str(other)]
    dg.quick()
    assert durable.read_text() == "keep"
    assert (protected / "empty").is_dir()
    assert not other.exists()


def test_deep_rechecks_exemption_added_during_confirmation(home, dg):
    parent = home / "research"
    protected = parent / "tests"
    protected.mkdir(parents=True)
    durable = protected / "important.txt"
    durable.write_text("keep")
    dg.save_tracked([record(parent, "chrome-profile")])
    def confirm(item):
        configure(home, [str(protected)])
        return True
    result = dg.deep(confirm)
    assert durable.read_text() == "keep"
    assert result["deep_deleted"] == 0


@pytest.mark.parametrize("paths", ["/tmp/no", ["relative"], ["/tmp/*"], [None], None])
def test_invalid_policy_prevents_wildcard_deletion(home, dg, paths):
    parent = home / "scratch"
    parent.mkdir()
    f = parent / "test_stale.py"
    f.write_text("keep")
    dg.save_tracked([record(str(parent) + "/*")])
    configure(home, paths)
    dg.quick()
    assert f.exists()


def load_plugin(monkeypatch):
    import sys
    source = Path(__file__).resolve().parents[2] / "plugins/disk-cleanup"
    name = "dc_exemption_plugin"
    spec = importlib.util.spec_from_file_location(name, source / "__init__.py", submodule_search_locations=[str(source)])
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


def test_registered_callbacks_keep_owner_profile_and_report_without_writes(home, tmp_path, monkeypatch, caplog):
    import logging
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    plugin = load_plugin(monkeypatch)
    protected = home / "tests"
    configure(home, [str(protected)])
    class Context:
        hooks = {}
        def register_hook(self, name, handler):
            self.hooks[name] = handler
        def register_command(self, name, handler, description):
            self.handler = handler
    ctx = Context()
    with caplog.at_level(logging.INFO):
        plugin.register(ctx)
    other = tmp_path / "other"
    other.mkdir()
    token = set_hermes_home_override(other)
    try:
        result = json.loads(ctx.handler("protection " + str(protected)))
        assert result["home"] == str(home)
        assert result["exempt"] is True
        assert result["policy_valid"] is True
        assert result["policy_version"] == 1
        assert "registered protection" in caplog.text
        # The actual registered lifecycle callback executes with its owner,
        # not the process/foreign active profile. No deletion test here.
        seen = []
        monkeypatch.setattr(plugin.dg, "quick", lambda: seen.append(plugin.dg.get_hermes_home()) or {"deleted": 0, "empty_dirs": 0})
        plugin._recent_test_tracks["owned-session"] = {"sentinel"}
        ctx.hooks["on_session_end"](session_id="owned-session")
        assert seen == [home]
        assert not (home / "disk-cleanup").exists()
        assert not (other / "disk-cleanup").exists()
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("relative", ["tests", "tests/sub/test_x.py", "tests-other/test_x.py"])
def test_exact_components_and_missing_roots(home, dg, relative):
    configure(home, [str(home / "tests")])
    assert dg.is_exempt(home / relative) == (not relative.startswith("tests-other"))
    assert dg.is_exempt(home, recursive=True)
    assert not dg.is_exempt(home)


def test_canonical_alias_and_lexical_symlink_out(home, dg):
    root = home / "tests"
    root.mkdir()
    alias = home / "alias"
    alias.symlink_to(root, target_is_directory=True)
    other = home / "outside"
    other.mkdir()
    (root / "out").symlink_to(other, target_is_directory=True)
    configure(home, [str(root)])
    assert dg.is_exempt(alias / "test_x.py")
    assert dg.is_exempt(root / "out" / "test_x.py")
    assert not dg.is_exempt(other / "test_x.py")


@pytest.mark.parametrize("category", ["test", "temp", "cron-output", "research", "chrome-profile", "other"])
def test_stale_categories_and_preview_cannot_override_exemption(home, dg, category):
    root = home / "tests"
    root.mkdir()
    p = root / "test_kept.py"
    p.write_text("keep")
    dg.save_tracked([record(p, category, 600 * 1024 * 1024)])
    configure(home, [str(root)])
    assert dg.dry_run() == ([], [])
    dg.deep(lambda _: True)
    assert p.exists()


@pytest.mark.parametrize("raw", ["[invalid", "42", "plugins: []", "plugins: {entries: null}", "plugins: {entries: {disk-cleanup: {settings: false}}}"])
def test_malformed_config_blocks_all_deletion(home, dg, raw):
    root = home / "scratch"
    root.mkdir()
    p = root / "test_kept.py"
    p.write_text("keep")
    dg.save_tracked([record(p), record(str(root) + "/*")])
    (home / "config.yaml").write_text(raw)
    assert not dg.protection_status(p)["policy_valid"]
    dg.quick()
    assert p.exists()


def test_unreadable_config_blocks(home, dg):
    (home / "config.yaml").mkdir()
    assert dg.is_exempt(home / "test.py")
    assert not dg.protection_status()["policy_valid"]


def test_same_module_uses_active_profile_policy(home, tmp_path, dg):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    configure(home, [str(home / "tests")])
    other = tmp_path / "profile2"
    other.mkdir()
    configure(other, [])
    assert dg.is_exempt(home / "tests")
    token = set_hermes_home_override(other)
    try:
        assert not dg.is_exempt(home / "tests")
    finally:
        reset_hermes_home_override(token)
    assert dg.is_exempt(home / "tests")


def test_wildcard_rechecks_policy_after_candidate_yield(home, dg, monkeypatch):
    parent = home / "scratch"
    parent.mkdir()
    p = parent / "test_kept.py"
    p.write_text("keep")
    dg.save_tracked([record(str(parent) + "/*")])
    original = dg._iter_safe_wildcard_files
    def changed(*args):
        for item in original(*args):
            configure(home, [str(parent)])
            yield item
    monkeypatch.setattr(dg, "_iter_safe_wildcard_files", changed)
    dg.quick()
    assert p.exists()


def test_empty_sweep_rechecks_before_rmdir(home, dg, monkeypatch):
    root = home / "tests"
    root.mkdir()
    original = Path.iterdir
    def changed(path):
        if path == root:
            configure(home, [str(root)])
        return original(path)
    monkeypatch.setattr(Path, "iterdir", changed)
    dg.quick()
    assert root.is_dir()


def test_invalid_policy_preserves_wildcard_record_for_recovery(home, dg):
    root = home / "scratch"
    root.mkdir()
    f = root / "test_file.py"
    f.write_text("temporary")
    entry = record(str(root) + "/*")
    dg.save_tracked([entry])
    configure(home, "invalid-list")
    dg.quick()
    assert dg.load_tracked() == [entry]
    configure(home, [])
    dg.quick()
    assert not f.exists()


def test_real_manager_loads_schema_and_profile_bound_diagnostic(home):
    from hermes_cli.plugins import PluginManager
    configure(home, [str(home / "tests")])
    raw = yaml.safe_load((home / "config.yaml").read_text())
    raw["plugins"]["enabled"] = ["disk-cleanup"]
    (home / "config.yaml").write_text(yaml.safe_dump(raw))
    mgr = PluginManager(scope_key=str(home))
    mgr.discover_and_load()
    loaded = mgr._plugins["disk-cleanup"]
    assert loaded.enabled, loaded.error
    assert loaded.manifest.config_schema["exempt_paths"]["type"] == "array"
    assert mgr.has_hook("on_session_end")
    handler = mgr._plugin_commands["disk-cleanup"]["handler"]
    result = json.loads(handler("protection " + str(home / "tests")))
    assert result["home"] == str(home)
    assert result["exempt"] is True
    assert not (home / "disk-cleanup").exists()
