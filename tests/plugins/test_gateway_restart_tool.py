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
