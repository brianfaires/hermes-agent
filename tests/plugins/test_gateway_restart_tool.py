from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_plugin_module():
    plugin_path = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "gateway-restart-tool"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("gateway_restart_tool_plugin", plugin_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_check_available_is_controlled_by_plugin_enablement_and_toolsets():
    module = _load_plugin_module()

    assert module._check_available() is True


def test_dry_run_does_not_require_profile_allow_list(monkeypatch):
    module = _load_plugin_module()
    monkeypatch.setattr(module, "_plugin_config", lambda: {})
    monkeypatch.setattr(module, "_active_profile_name", lambda: "research")
    monkeypatch.setattr(module, "_append_audit", lambda record: None)
    monkeypatch.setattr(module, "_audit_path", lambda: Path("/tmp/gateway-restart-tool.jsonl"))
    monkeypatch.setattr(module, "_resolve_runner", lambda: None)

    result = json.loads(
        module._handle_request_gateway_restart(
            {
                "reason": "config reload",
                "confirm": "restart gateway",
                "dry_run": True,
            }
        )
    )

    assert result["ok"] is True
    assert result["profile"] == "research"
    assert "allowed_profiles" not in result
    assert result["runner_available"] is False


def test_cross_profile_restart_requires_explicit_target_allow_list(monkeypatch):
    module = _load_plugin_module()
    monkeypatch.setattr(module, "_plugin_config", lambda: {})
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", lambda record: None)

    result = json.loads(
        module._handle_request_gateway_restart(
            {
                "reason": "reload target configuration",
                "confirm": "restart gateway",
                "target_profile": "research",
            }
        )
    )

    assert result == {
        "ok": False,
        "error": "target_profile_not_allowed",
        "profile": "ops",
        "target_profile": "research",
        "allowed_target_profiles": ["ops"],
    }


def test_cross_profile_restart_uses_profile_scoped_cli(monkeypatch):
    module = _load_plugin_module()
    audits = []
    monkeypatch.setattr(
        module,
        "_plugin_config",
        lambda: {"allowed_target_profiles": ["research"]},
    )
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", audits.append)
    monkeypatch.setattr(module, "_read_last_restart_time", lambda: 0.0)
    monkeypatch.setattr(module, "_write_last_restart_time", lambda now: None)
    monkeypatch.setattr(module, "_audit_path", lambda: Path("/tmp/gateway-restart-tool.jsonl"))
    monkeypatch.setattr(module, "_spawn_profile_restart", lambda profile: 4321)

    result = json.loads(
        module._handle_request_gateway_restart(
            {
                "reason": "reload target configuration",
                "confirm": "restart gateway",
                "target_profile": "research",
            }
        )
    )

    assert result["ok"] is True
    assert result["target_profile"] == "research"
    assert result["dispatch"] == "profile_cli"
    assert result["child_pid"] == 4321
    assert audits[-1]["decision"] == "scheduled"


def test_profile_restart_child_does_not_inherit_gateway_marker(monkeypatch):
    module = _load_plugin_module()
    captured = {}

    class FakeProcess:
        pid = 4321

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)

    assert module._spawn_profile_restart("research") == 4321
    assert captured["command"][-4:] == ["-p", "research", "gateway", "restart"]
    assert captured["kwargs"]["env"]["HERMES_NONINTERACTIVE"] == "1"
    assert "_HERMES_GATEWAY" not in captured["kwargs"]["env"]
