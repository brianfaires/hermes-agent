"""Tests for the disk-cleanup plugin.

Covers the bundled plugin at ``plugins/disk-cleanup/``:

  * ``disk_cleanup`` library: track / forget / dry_run / quick / status,
    ``is_safe_path`` and ``guess_category`` filtering.
  * Plugin ``__init__``: ``post_tool_call`` hook auto-tracks files created
    by ``write_file`` / ``terminal``; ``on_session_end`` hook runs quick
    cleanup when anything was tracked during the turn.
  * Slash command handler: status / dry-run / quick / track / forget /
    unknown subcommand behaviours.
  * Bundled-plugin discovery via ``PluginManager.discover_and_load``.
"""

import importlib
import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test.

    The global hermetic fixture already redirects HERMES_HOME to a tempdir,
    but we want the plugin to work with a predictable subpath. We reset
    HERMES_HOME here for clarity.
    """
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


def _load_lib():
    """Import the plugin's library module directly from the repo path."""
    repo_root = Path(__file__).resolve().parents[2]
    lib_path = repo_root / "plugins" / "disk-cleanup" / "disk_cleanup.py"
    spec = importlib.util.spec_from_file_location(
        "disk_cleanup_under_test", lib_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_plugin_init():
    """Import the plugin's __init__.py (which depends on the library)."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_dir = repo_root / "plugins" / "disk-cleanup"
    # Use the PluginManager's module naming convention so relative imports work.
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.disk_cleanup",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    # Ensure parent namespace package exists for the relative `. import disk_cleanup`
    import types
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.disk_cleanup"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.disk_cleanup"] = mod
    spec.loader.exec_module(mod)
    return mod


def _set_age_in_days(path: Path, days: int):
    ts = path.stat().st_mtime - (days * 24 * 60 * 60)
    os.utime(path, (ts, ts))


# ---------------------------------------------------------------------------
# Library tests
# ---------------------------------------------------------------------------

class TestIsSafePath:
    def test_accepts_path_under_hermes_home(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "subdir" / "file.txt"
        p.parent.mkdir()
        p.write_text("x")
        assert dg.is_safe_path(p) is True

    def test_rejects_outside_hermes_home(self, _isolate_env):
        dg = _load_lib()
        assert dg.is_safe_path(Path("/etc/passwd")) is False


class TestGuessCategory:
    def test_test_prefix(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "test_foo.py"
        p.write_text("x")
        assert dg.guess_category(p) == "test"

    def test_tmp_prefix(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "tmp_foo.log"
        p.write_text("x")
        assert dg.guess_category(p) == "test"

    def test_dot_test_suffix(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "mything.test.js"
        p.write_text("x")
        assert dg.guess_category(p) == "test"

    def test_skips_protected_top_level(self, _isolate_env):
        dg = _load_lib()
        logs_dir = _isolate_env / "logs"
        logs_dir.mkdir()
        p = logs_dir / "test_log.txt"
        p.write_text("x")
        # Even though it matches test_* pattern, logs/ is excluded.
        assert dg.guess_category(p) is None

    def test_cron_subtree_categorised(self, _isolate_env):
        dg = _load_lib()
        # Only files under ``cron/output/`` are disposable run artifacts.
        output_dir = _isolate_env / "cron" / "output" / "job_123"
        output_dir.mkdir(parents=True)
        p = output_dir / "run.md"
        p.write_text("x")
        assert dg.guess_category(p) == "cron-output"


    def test_cronjobs_top_level_not_tracked(self, _isolate_env):
        """The legacy ``cronjobs`` alias is also control-plane at the top."""
        dg = _load_lib()
        cron_dir = _isolate_env / "cronjobs"
        cron_dir.mkdir()
        p = cron_dir / "jobs.json"
        p.write_text("[]")
        assert dg.guess_category(p) is None

    def test_ordinary_file_returns_none(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "notes.md"
        p.write_text("x")
        assert dg.guess_category(p) is None


class TestStaleCronEntryMigration:
    """Regression tests for #37721 — stale cron-output entries in tracked.json."""

    def test_quick_skips_stale_cron_output_for_jobs_json(self, _isolate_env):
        """A stale tracked.json entry with category="cron-output" for
        cron/jobs.json must NOT be deleted by quick().

        This is the exact scenario from #37721: an old tracked.json has
        {"path": ".../cron/jobs.json", "category": "cron-output"} which
        would pass the delete filter but must be skipped because
        guess_category() now returns None for non-output cron paths.
        """
        dg = _load_lib()
        cron_dir = _isolate_env / "cron"
        cron_dir.mkdir()
        jobs_json = cron_dir / "jobs.json"
        jobs_json.write_text('{"jobs": []}')

        # Simulate a stale tracked.json entry from before #34840 by
        # directly writing the tracked file (track() would reject it).
        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(json.dumps([{
            "path": str(jobs_json),
            "category": "cron-output",
            "timestamp": "2025-01-01T00:00:00+00:00",  # very old
            "size": 123,
        }]))

        summary = dg.quick()
        assert summary["deleted"] == 0, "cron/jobs.json must not be deleted"
        assert jobs_json.exists(), "jobs.json must still exist"
        # The stale entry should have been dropped from tracking.
        remaining = json.loads(tracked_file.read_text())
        assert len(remaining) == 0


    def test_dry_run_omits_stale_cron_output(self, _isolate_env):
        """dry_run() should also skip stale cron-output entries."""
        dg = _load_lib()
        cron_dir = _isolate_env / "cron"
        cron_dir.mkdir()
        jobs_json = cron_dir / "jobs.json"
        jobs_json.write_text("[]")

        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(json.dumps([{
            "path": str(jobs_json),
            "category": "cron-output",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "size": 123,
        }]))

        auto, prompt = dg.dry_run()
        assert len(auto) == 0, "stale cron-output for jobs.json must not appear"
        assert len(prompt) == 0

    def test_legitimate_cron_output_still_deleted(self, _isolate_env):
        """A valid cron-output entry under cron/output/ must still be deleted."""
        dg = _load_lib()
        output_dir = _isolate_env / "cron" / "output" / "job_1"
        output_dir.mkdir(parents=True)
        run_md = output_dir / "run.md"
        run_md.write_text("x")

        # Old enough to be deleted (>14 days)
        from datetime import datetime, timezone, timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()

        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(json.dumps([{
            "path": str(run_md),
            "category": "cron-output",
            "timestamp": old_ts,
            "size": 10,
        }]))

        summary = dg.quick()
        assert summary["deleted"] == 1, "valid old cron-output should be deleted"
        assert not run_md.exists()


class TestDurableScriptMigration:
    @pytest.mark.parametrize(
        "parts",
        [
            ("scripts",),
            ("profiles", "worker", "scripts"),
        ],
    )
    def test_quick_drops_tracked_script_root_without_deleting_it(
        self, _isolate_env, parts
    ):
        dg = _load_lib()
        scripts_dir = _isolate_env.joinpath(*parts)
        scripts_dir.mkdir(parents=True)

        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(json.dumps([{
            "path": str(scripts_dir),
            "category": "temp",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "size": 0,
        }]))

        summary = dg.quick()
        assert summary["deleted"] == 0
        assert scripts_dir.exists(), "durable scripts root must not be deleted"
        assert json.loads(tracked_file.read_text()) == []

    def test_quick_drops_old_temp_entry_under_active_profile_scripts(
        self, _isolate_env
    ):
        dg = _load_lib()
        script = _isolate_env / "scripts" / "sync_state.tmp"
        script.parent.mkdir()
        script.write_text("durable\n")

        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(json.dumps([{
            "path": str(script),
            "category": "temp",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "size": 8,
        }]))

        summary = dg.quick()
        assert summary["deleted"] == 0
        assert script.exists(), "active-profile scripts must not be deleted"
        assert json.loads(tracked_file.read_text()) == []

    def test_dry_run_omits_old_temp_entry_under_nested_profile_scripts(
        self, _isolate_env
    ):
        dg = _load_lib()
        script = (
            _isolate_env
            / "profiles"
            / "worker"
            / "scripts"
            / "sync_state.tmp"
        )
        script.parent.mkdir(parents=True)
        script.write_text("durable\n")

        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(json.dumps([{
            "path": str(script),
            "category": "temp",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "size": 8,
        }]))

        auto, prompt = dg.dry_run()
        assert auto == []
        assert prompt == []


class TestTrackForgetQuick:
    def test_track_then_quick_deletes_test(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "test_a.py"
        p.write_text("x")
        assert dg.track(str(p), "test", silent=True) is True
        summary = dg.quick()
        assert summary["deleted"] == 1
        assert not p.exists()


    def test_forget_removes_entry(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "keep.tmp"
        p.write_text("x")
        dg.track(str(p), "temp", silent=True)
        assert dg.forget(str(p)) == 1
        assert p.exists()  # forget does NOT delete the file

    def test_track_and_forget_wildcard_policy(self, _isolate_env):
        dg = _load_lib()
        root = _isolate_env / "audio_cache"
        root.mkdir()
        policy = f"{root}/*"

        assert dg.track(policy, "temp", silent=True) is True
        assert dg.load_tracked()[0]["path"] == policy
        assert dg.forget(policy) == 1
        assert dg.load_tracked() == []

    def test_track_rejects_durable_script_wildcard_policy(self, _isolate_env):
        dg = _load_lib()
        scripts_dir = _isolate_env / "scripts"
        scripts_dir.mkdir()

        assert dg.track(f"{scripts_dir}/*", "temp", silent=True) is False
        assert dg.load_tracked() == []

    @pytest.mark.parametrize("directory_name", ["?", "[abc]"])
    def test_track_rejects_unsupported_wildcard_shapes(
        self, _isolate_env, directory_name
    ):
        dg = _load_lib()
        unsupported_parent = _isolate_env / "audio_cache" / directory_name
        unsupported_parent.mkdir(parents=True)

        assert dg.track(
            f"{unsupported_parent}/*", "temp", silent=True
        ) is False
        assert dg.load_tracked() == []

    @pytest.mark.parametrize(
        "parts",
        [
            (),
            ("logs",),
            ("memories",),
            ("sessions",),
            ("skills",),
            ("plugins",),
            ("profiles",),
            ("cron",),
            ("cron", "control"),
        ],
    )
    def test_track_rejects_durable_wildcard_policy_roots(
        self, _isolate_env, parts
    ):
        dg = _load_lib()
        root = _isolate_env.joinpath(*parts)
        root.mkdir(parents=True, exist_ok=True)

        assert dg.track(f"{root}/*", "temp", silent=True) is False
        assert dg.load_tracked() == []

    def test_quick_wildcard_recursively_prunes_old_files_and_keeps_policy(
        self, _isolate_env
    ):
        dg = _load_lib()
        root = _isolate_env / "audio_cache"
        nested = root / "job" / "chunks"
        nested.mkdir(parents=True)
        old_file = nested / "old.wav"
        old_file.write_text("old")
        _set_age_in_days(old_file, 10)
        young_file = root / "fresh.wav"
        young_file.write_text("young")

        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        policy = f"{root}/*"
        tracked_file.write_text(json.dumps([{
            "path": policy,
            "category": "temp",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "size": 0,
        }]))

        summary = dg.quick()

        assert summary["deleted"] == 1
        assert not old_file.exists()
        assert not nested.exists()
        assert young_file.exists()
        assert root.exists()
        assert json.loads(tracked_file.read_text())[0]["path"] == policy

    def test_quick_drops_logs_wildcard_policy_without_deleting_descendants(
        self, _isolate_env
    ):
        dg = _load_lib()
        root = _isolate_env / "logs"
        root.mkdir()
        old_log = root / "old.log"
        old_log.write_text("durable log")
        _set_age_in_days(old_log, 10)

        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(json.dumps([{
            "path": f"{root}/*",
            "category": "temp",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "size": 0,
        }]))

        summary = dg.quick()

        assert summary["deleted"] == 0
        assert old_log.exists()
        assert json.loads(tracked_file.read_text()) == []

    def test_quick_drops_logs_wildcard_syntax_without_deleting_literal_star(
        self, _isolate_env
    ):
        dg = _load_lib()
        root = _isolate_env / "logs"
        literal_star = root / "*"
        literal_star.mkdir(parents=True)
        durable_log = literal_star / "old.log"
        durable_log.write_text("durable log")
        _set_age_in_days(durable_log, 10)

        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(json.dumps([{
            "path": f"{root}/*",
            "category": "temp",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "size": 0,
        }]))

        summary = dg.quick()

        assert summary["deleted"] == 0
        assert durable_log.exists()
        assert json.loads(tracked_file.read_text()) == []

    @pytest.mark.parametrize("policy_first", [True, False])
    def test_quick_preserves_wildcard_root_when_old_parent_entry_exists(
        self, _isolate_env, policy_first
    ):
        dg = _load_lib()
        root = _isolate_env / "audio_cache"
        root.mkdir()
        young_file = root / "fresh.wav"
        young_file.write_text("young")
        policy = f"{root}/*"

        wildcard_item = {
            "path": policy,
            "category": "temp",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "size": 0,
        }
        parent_item = {
            "path": str(root),
            "category": "temp",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "size": 0,
        }
        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(json.dumps(
            [wildcard_item, parent_item]
            if policy_first
            else [parent_item, wildcard_item]
        ))

        summary = dg.quick()

        assert summary["deleted"] == 0
        assert root.exists()
        assert young_file.exists()
        remaining = json.loads(tracked_file.read_text())
        assert [item["path"] for item in remaining] == [policy]

    @pytest.mark.parametrize("policy_first", [True, False])
    @pytest.mark.parametrize("tracked_kind", ["file", "directory"])
    def test_quick_drops_ordinary_entries_below_valid_wildcard_root(
        self, _isolate_env, policy_first, tracked_kind
    ):
        dg = _load_lib()
        root = _isolate_env / "audio_cache"
        root.mkdir()
        policy = f"{root}/*"

        if tracked_kind == "file":
            young_file = root / "fresh.wav"
            young_file.write_text("young")
            stale_path = young_file
        else:
            stale_path = root / "job"
            stale_path.mkdir()
            young_file = stale_path / "fresh.wav"
            young_file.write_text("young")

        wildcard_item = {
            "path": policy,
            "category": "temp",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "size": 0,
        }
        stale_ordinary_item = {
            "path": str(stale_path),
            "category": "temp",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "size": 0,
        }
        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(json.dumps(
            [wildcard_item, stale_ordinary_item]
            if policy_first
            else [stale_ordinary_item, wildcard_item]
        ))

        summary = dg.quick()

        assert summary["deleted"] == 0
        assert root.exists()
        assert young_file.exists()
        remaining = json.loads(tracked_file.read_text())
        assert [item["path"] for item in remaining] == [policy]

    def test_quick_wildcard_ignores_symlink_escape(self, _isolate_env):
        dg = _load_lib()
        root = _isolate_env / "audio_cache"
        root.mkdir()
        outside = _isolate_env.parent / "outside-old.wav"
        outside.write_text("outside")
        _set_age_in_days(outside, 10)
        link = root / "escape.wav"
        link.symlink_to(outside)

        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(json.dumps([{
            "path": f"{root}/*",
            "category": "temp",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "size": 0,
        }]))

        summary = dg.quick()

        assert summary["deleted"] == 0
        assert outside.exists()
        assert link.exists()

    def test_quick_cron_output_wildcard_prunes_old_descendants(
        self, _isolate_env
    ):
        dg = _load_lib()
        root = _isolate_env / "cron" / "output"
        run_dir = root / "job-1"
        run_dir.mkdir(parents=True)
        old_file = run_dir / "run.md"
        old_file.write_text("old")
        _set_age_in_days(old_file, 20)
        young_file = run_dir / "fresh.md"
        young_file.write_text("young")

        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        policy = f"{root}/*"
        tracked_file.write_text(json.dumps([{
            "path": policy,
            "category": "cron-output",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "size": 0,
        }]))

        summary = dg.quick()

        assert summary["deleted"] == 1
        assert not old_file.exists()
        assert young_file.exists()
        assert root.exists()
        assert json.loads(tracked_file.read_text())[0]["path"] == policy


class TestStatus:
    def test_empty_status(self, _isolate_env):
        dg = _load_lib()
        s = dg.status()
        assert s["total_tracked"] == 0
        assert s["top10"] == []

    def test_status_with_entries(self, _isolate_env):
        dg = _load_lib()
        p = _isolate_env / "big.tmp"
        p.write_text("y" * 100)
        dg.track(str(p), "temp", silent=True)
        s = dg.status()
        assert s["total_tracked"] == 1
        assert len(s["top10"]) == 1
        rendered = dg.format_status(s)
        assert "temp" in rendered
        assert "big.tmp" in rendered


class TestDryRun:
    def test_classifies_by_category(self, _isolate_env):
        dg = _load_lib()
        test_f = _isolate_env / "test_x.py"
        test_f.write_text("x")
        big = _isolate_env / "big.bin"
        big.write_bytes(b"z" * 10)
        dg.track(str(test_f), "test", silent=True)
        dg.track(str(big), "other", silent=True)
        auto, prompt = dg.dry_run()
        # test → auto, other → neither (doesn't hit any rule)
        assert any(i["path"] == str(test_f) for i in auto)

    def test_wildcard_dry_run_lists_eligible_files_not_policy(
        self, _isolate_env
    ):
        dg = _load_lib()
        root = _isolate_env / "audio_cache"
        root.mkdir()
        old_file = root / "old.wav"
        old_file.write_text("old")
        _set_age_in_days(old_file, 10)
        young_file = root / "fresh.wav"
        young_file.write_text("young")
        policy = f"{root}/*"

        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        tracked_file.parent.mkdir(parents=True, exist_ok=True)
        tracked_file.write_text(json.dumps([{
            "path": policy,
            "category": "temp",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "size": 0,
        }]))

        auto, prompt = dg.dry_run()

        assert [item["path"] for item in auto] == [str(old_file)]
        assert policy not in [item["path"] for item in auto]
        assert str(young_file) not in [item["path"] for item in auto]
        assert prompt == []


# ---------------------------------------------------------------------------
# Plugin hooks tests
# ---------------------------------------------------------------------------

class TestPostToolCallHook:
    def test_write_file_test_pattern_tracked(self, _isolate_env):
        pi = _load_plugin_init()
        p = _isolate_env / "test_created.py"
        p.write_text("x")
        pi._on_post_tool_call(
            tool_name="write_file",
            args={"path": str(p), "content": "x"},
            result="OK",
            task_id="t1", session_id="s1",
        )
        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        data = json.loads(tracked_file.read_text())
        assert len(data) == 1
        assert data[0]["category"] == "test"


    def test_terminal_command_picks_up_paths(self, _isolate_env):
        pi = _load_plugin_init()
        p = _isolate_env / "tmp_created.log"
        p.write_text("x")
        pi._on_post_tool_call(
            tool_name="terminal",
            args={"command": f"touch {p}"},
            result=f"created {p}\n",
            task_id="t3", session_id="s3",
        )
        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        data = json.loads(tracked_file.read_text())
        assert any(Path(i["path"]) == p.resolve() for i in data)

    def test_ignores_unrelated_tool(self, _isolate_env):
        pi = _load_plugin_init()
        pi._on_post_tool_call(
            tool_name="read_file",
            args={"path": str(_isolate_env / "test_x.py")},
            result="contents",
            task_id="t4", session_id="s4",
        )
        # read_file should never trigger tracking.
        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        assert not tracked_file.exists() or tracked_file.read_text().strip() == "[]"


class TestOnSessionEndHook:
    def test_runs_quick_when_test_files_tracked(self, _isolate_env):
        pi = _load_plugin_init()
        p = _isolate_env / "test_cleanup.py"
        p.write_text("x")
        pi._on_post_tool_call(
            tool_name="write_file",
            args={"path": str(p), "content": "x"},
            result="OK",
            task_id="", session_id="s1",
        )
        assert p.exists()
        pi._on_session_end(session_id="s1", completed=True, interrupted=False)
        assert not p.exists(), "test file should be auto-deleted"

    def test_write_file_durable_scripts_test_file_persists(self, _isolate_env):
        pi = _load_plugin_init()
        p = _isolate_env / "scripts" / "test_cron_calendar_recurring_sync.py"
        p.parent.mkdir()
        p.write_text("x")

        pi._on_post_tool_call(
            tool_name="write_file",
            args={"path": str(p), "content": "x"},
            result="OK",
            task_id="", session_id="s-durable-script",
        )
        pi._on_session_end(
            session_id="s-durable-script",
            completed=True,
            interrupted=False,
        )

        assert p.exists(), "durable scripts/test_*.py file must not be deleted"
        tracked_file = _isolate_env / "disk-cleanup" / "tracked.json"
        if tracked_file.exists():
            data = json.loads(tracked_file.read_text())
            assert all(Path(item["path"]) != p.resolve() for item in data)

    def test_noop_when_no_test_tracked(self, _isolate_env):
        pi = _load_plugin_init()
        # Nothing tracked → on_session_end should not raise.
        pi._on_session_end(session_id="empty", completed=True, interrupted=False)


# ---------------------------------------------------------------------------
# Slash command
# ---------------------------------------------------------------------------

class TestSlashCommand:
    def test_help(self, _isolate_env):
        pi = _load_plugin_init()
        out = pi._handle_slash("help")
        assert "disk-cleanup" in out
        assert "status" in out


    def test_unknown_subcommand(self, _isolate_env):
        pi = _load_plugin_init()
        out = pi._handle_slash("foobar")
        assert "Unknown subcommand" in out


# ---------------------------------------------------------------------------
# Bundled-plugin discovery
# ---------------------------------------------------------------------------

class TestBundledDiscovery:
    def _write_enabled_config(self, hermes_home, names):
        """Write plugins.enabled allow-list to config.yaml."""
        import yaml
        cfg_path = hermes_home / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({"plugins": {"enabled": list(names)}}))

    def test_disk_cleanup_discovered_but_not_loaded_by_default(self, _isolate_env):
        """Bundled plugins are discovered but NOT loaded without opt-in."""
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        # Discovered — appears in the registry
        assert "disk-cleanup" in mgr._plugins
        loaded = mgr._plugins["disk-cleanup"]
        assert loaded.manifest.source == "bundled"
        # But NOT enabled — no hooks or commands registered
        assert not loaded.enabled
        assert loaded.error and "not enabled" in loaded.error


    def test_disabled_beats_enabled(self, _isolate_env):
        """plugins.disabled wins even if the plugin is also in plugins.enabled."""
        import yaml
        cfg_path = _isolate_env / "config.yaml"
        cfg_path.write_text(yaml.safe_dump({
            "plugins": {
                "enabled": ["disk-cleanup"],
                "disabled": ["disk-cleanup"],
            }
        }))
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        loaded = mgr._plugins["disk-cleanup"]
        assert not loaded.enabled
        assert loaded.error == "disabled via config"

    def test_memory_and_context_engine_subdirs_skipped(self, _isolate_env):
        """Bundled scan must NOT pick up plugins/memory or plugins/context_engine
        as top-level plugins — they have their own discovery paths."""
        self._write_enabled_config(
            _isolate_env, ["memory", "context_engine", "disk-cleanup"]
        )
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        assert "memory" not in mgr._plugins
        assert "context_engine" not in mgr._plugins
