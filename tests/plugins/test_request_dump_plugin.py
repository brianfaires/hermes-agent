from __future__ import annotations

import importlib
import sqlite3
import stat
from datetime import datetime as real_datetime

from gateway.platforms.base import BasePlatformAdapter
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

MODULE = "plugins.debug.request_dump"


def load_plugin():
    return importlib.reload(importlib.import_module(MODULE))


def test_skills_footprint_is_separate_from_prompt_footprint():
    plugin = load_plugin()
    prompt = "prefix<available_skills>abc</available_skills>suffix"
    header = plugin._header(
        {"system_prompt": prompt},
        enabled=[],
        disabled=[],
        tools=[],
    )
    assert f"prompt_footprint: {len(prompt) - 3} chars" in header
    assert "skills_footprint: 3 chars" in header
    assert header.index("tools_footprint:") < header.index("skills_footprint:")


def make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE sessions (
        id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT, model TEXT,
        model_config TEXT, system_prompt TEXT, parent_session_id TEXT,
        started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
        message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
        input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
        reasoning_tokens INTEGER DEFAULT 0, cwd TEXT, billing_provider TEXT,
        billing_base_url TEXT, billing_mode TEXT, estimated_cost_usd REAL,
        actual_cost_usd REAL, cost_status TEXT, cost_source TEXT,
        pricing_version TEXT, title TEXT, api_call_count INTEGER DEFAULT 0,
        handoff_state TEXT, handoff_platform TEXT, handoff_error TEXT,
        rewind_count INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0
    )""")
    conn.executemany(
        "INSERT INTO sessions (id, source, model, model_config, system_prompt, started_at, title, cwd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


class FixedDatetime:
    @classmethod
    def now(cls):
        return real_datetime(2026, 7, 12, 21, 30, 45).astimezone()

    @classmethod
    def fromtimestamp(cls, value, tz=None):
        return real_datetime.fromtimestamp(value, tz)


def configure(monkeypatch, plugin, enabled=("file", "terminal"), disabled=("browser",)):
    calls = {}
    import hermes_cli.config
    import hermes_cli.tools_config
    import model_tools

    monkeypatch.setattr(hermes_cli.config, "load_config", lambda: {
        "agent": {"disabled_toolsets": list(disabled)}, "marker": "config"
    })

    def platform_tools(config, platform):
        calls["config"] = config
        calls["platform"] = platform
        return set(enabled)

    monkeypatch.setattr(hermes_cli.tools_config, "_get_platform_tools", platform_tools)

    def definitions(**kwargs):
        calls["definitions"] = kwargs
        return [{"type": "function", "function": {
            "name": "terminal", "description": 'Run\\nSay \\"hello\\"'
        }}]

    monkeypatch.setattr(model_tools, "get_tool_definitions", definitions)
    return calls


def test_registration_has_command_and_no_hook():
    plugin = load_plugin()
    manager = PluginManager()
    plugin.register(PluginContext(PluginManifest(name="request-dump"), manager))
    assert "dump-system-prompt" in manager._plugin_commands
    assert manager._plugin_commands["dump-system-prompt"]["args_hint"] == ""
    assert manager._hooks == {}


def test_no_data_response_has_no_success_caveat(tmp_path, monkeypatch):
    plugin = load_plugin()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(plugin, "_dump_root", lambda: tmp_path / "dump-system-prompt")
    result = plugin.dump_latest()
    assert "No persisted session" in result
    assert "current profile/tool configuration" not in result
    assert not (tmp_path / "dump-system-prompt").exists()


def test_newest_nonempty_full_prompt_tools_headers_permissions_and_collision(tmp_path, monkeypatch):
    plugin = load_plugin()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "work")
    monkeypatch.setattr(plugin, "_dump_root", lambda: tmp_path / "dump-system-prompt")
    monkeypatch.setattr(plugin, "datetime", FixedDatetime)
    long_prompt = "START\n" + ("x" * 9000) + "\nEND"
    make_db(tmp_path / "state.db", [
        ("older", "cli", "old-model", None, "old prompt", 1000, "Old", "/old"),
        ("new/session identifier!", "discord", "new-model", '{"temperature":0}', long_prompt, 2000, "Newest", "/project"),
        ("latest-empty", "telegram", "ignored", None, "", 3000, "Empty", None),
    ])
    calls = configure(monkeypatch, plugin)

    result = plugin.dump_latest()
    output = tmp_path / "dump-system-prompt"
    full = output / "work-20260712-213045-n_identifier-1.txt"
    assert list(output.iterdir()) == [full]
    assert f"`{full}`" in result
    assert result == f"System prompt written: `{full}`"
    attachments, visible = BasePlatformAdapter.extract_local_files(result)
    assert attachments == [] and str(full) in visible

    full_text = full.read_text()
    assert long_prompt in full_text
    assert "truncat" not in full_text.lower()
    assert "Current assembled tool definitions:" in full_text
    assert '"name": "terminal"' in full_text
    assert 'Run\nSay "hello"' in full_text
    assert r"Run\n" not in full_text and r'\"hello\"' not in full_text
    assert full_text.startswith("Hermes system prompt estimate\n")
    assert "profile: work" in full_text
    assert f"HERMES_HOME: {tmp_path}" in full_text
    assert "session_id: new/session identifier!" in full_text
    assert "title: Newest" in full_text and "model: new-model" in full_text
    assert "source/platform: discord" in full_text
    assert "session_cwd: /project" in full_text
    assert "enabled_toolsets: file, terminal" in full_text
    assert "disabled_toolsets: browser" in full_text
    assert "tool_count: 1" in full_text and "terminal:" in full_text
    assert "prompt_footprint:" in full_text
    assert "tools_footprint:" in full_text
    assert "estimated_total_footprint:" in full_text
    assert "of total)" in full_text
    assert "Run\nSay" in full_text
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(full.stat().st_mode) == 0o600
    assert calls["platform"] == "discord"
    assert calls["config"]["marker"] == "config"
    assert calls["definitions"] == {
        "enabled_toolsets": ["file", "terminal"],
        "disabled_toolsets": ["browser"],
        "quiet_mode": True,
        "skip_tool_search_assembly": False,
    }

    second = plugin.dump_latest()
    assert "`" + str(output / "work-20260712-213045-n_identifier-2.txt") + "`" in second
    assert len(list(output.iterdir())) == 2
