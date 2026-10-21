"""Selected authorized schemas stay direct without changing a durable session prefix."""
import cli
import gateway.run
import run_agent
import tui_gateway.server

import json
from types import SimpleNamespace

import pytest

from agent.agent_init import _load_tools
from hermes_state import SessionDB
from tools.mcp_tool_agent import persist_agent_tool_names, refresh_agent_mcp_tools, restore_agent_tool_prefix
from tools.registry import registry
from tools.tool_search import dispatch_tool_search


@pytest.fixture
def pin_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name, allowed in (("pin_selected", True), ("pin_other", True), ("pin_unavailable", False)):
        registry.register(name=name, toolset="mcp-pin-contract", schema={
            "name": name, "description": f"Read {name} records", "parameters": {"type": "object", "properties": {}}},
            handler=lambda args, **kw: '{"ok":true}', check_fn=lambda ok=allowed: ok)
    registry.register(name="pin_forbidden", toolset="mcp-pin-forbidden", schema={
        "name": "pin_forbidden", "description": "Forbidden records", "parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: '{"ok":true}')
    _write_pins(tmp_path, "pin_selected")
    return tmp_path


def _write_pins(home, selected):
    (home / "config.yaml").write_text(
        "tools:\n  tool_search:\n    enabled: on\n    pinned:\n"
        f"      discord: [{selected}, pin_unavailable, pin_forbidden, missing_tool]\n"
        "      telegram: [pin_other]\n", encoding="utf-8")


def _agent(platform="discord", disabled=None):
    agent = SimpleNamespace(platform=platform, quiet_mode=True, enabled_toolsets=["mcp-pin-contract"],
                            disabled_toolsets=disabled or [], tools=[], valid_tool_names=set())
    _load_tools(agent, agent.enabled_toolsets, agent.disabled_toolsets)
    return agent


def test_platform_pins_only_promote_allowed_schemas_and_keep_the_rest_discoverable(pin_home):
    discord = _agent()
    assert "pin_selected" in discord.valid_tool_names
    assert not ({"pin_other", "pin_unavailable", "pin_forbidden", "missing_tool"} & discord.valid_tool_names)
    assert "tool_search" in discord.valid_tool_names
    import model_tools
    raw = model_tools.get_tool_definitions(enabled_toolsets=discord.enabled_toolsets,
                                          quiet_mode=True, skip_tool_search_assembly=True)
    result = json.loads(dispatch_tool_search({"queries": ["pin_other"]}, current_tool_defs=raw))
    assert "pin_other" in result["results"][0]["matches"]
    assert "pin_forbidden" not in json.dumps(result)
    telegram = _agent("telegram")
    assert "pin_other" in telegram.valid_tool_names and "pin_selected" not in telegram.valid_tool_names
    assert _agent().tools == discord.tools
    assert "pin_selected" not in _agent(disabled=["mcp-pin-contract"]).valid_tool_names


@pytest.mark.parametrize("all_pinned", [False, True, None])
def test_resume_and_live_refresh_keep_saved_bytes_when_platform_pins_change(pin_home, all_pinned):
    if all_pinned is None:
        (pin_home / "config.yaml").write_text("tools:\n  tool_search:\n    enabled: on\n    pinned: {}\n")
    elif all_pinned:
        _write_pins(pin_home, "pin_selected, pin_other")
    db = SessionDB(db_path=pin_home / "sessions.db")
    try:
        db.create_session("pin-contract", "discord")
        original = _agent()
        original._session_db, original.session_id = db, "pin-contract"
        persist_agent_tool_names(original)
        saved = json.loads(db.get_session("pin-contract")["tool_names"])
        before = json.dumps(original.tools, separators=(",", ":"))
        _write_pins(pin_home, "pin_other")
        refresh_agent_mcp_tools(original, preserve_prefix=True)
        assert json.dumps(original.tools, separators=(",", ":")) == before
        resumed = _agent()
        resumed._session_db, resumed.session_id = db, "pin-contract"
        restore_agent_tool_prefix(resumed, saved)
        assert json.dumps(resumed.tools, separators=(",", ":")) == before
        refresh_agent_mcp_tools(resumed, preserve_prefix=True)
        assert json.dumps(resumed.tools, separators=(",", ":")) == before
        hopped = _agent("telegram")
        hopped._session_db, hopped.session_id = db, "pin-contract"
        restore_agent_tool_prefix(hopped, saved)
        assert json.dumps(hopped.tools, separators=(",", ":")) == before
        refresh_agent_mcp_tools(hopped, preserve_prefix=True)
        assert json.dumps(hopped.tools, separators=(",", ":")) == before
        denied = _agent(disabled=["mcp-pin-contract"])
        restore_agent_tool_prefix(denied, saved)
        assert not ({"pin_selected", "pin_other"} & denied.valid_tool_names)
    finally:
        db.close()
