from __future__ import annotations

import importlib.util
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
    monkeypatch.setattr(module, "_read_last_restart_time", lambda target, source: 0.0)
    monkeypatch.setattr(module, "_write_last_restart_time", lambda target, now: None)
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


def test_cooldown_is_scoped_to_the_target_profile(monkeypatch, tmp_path):
    module = _load_plugin_module()
    monkeypatch.setattr(module, "_state_path", lambda: tmp_path / "restart-state.json")

    module._write_last_restart_time("default", 100.0)

    assert module._read_last_restart_time("default", "ops") == 100.0
    assert module._read_last_restart_time("ops", "ops") == 0.0


def test_concurrent_restart_requests_reserve_cooldown_once(monkeypatch, tmp_path):
    module = _load_plugin_module()
    monkeypatch.setattr(module, "_state_path", lambda: tmp_path / "restart-state.json")
    monkeypatch.setattr(
        module,
        "_plugin_config",
        lambda: {"allowed_target_profiles": ["research"], "cooldown_seconds": 60},
    )
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", lambda record: None)
    monkeypatch.setattr(module, "_audit_path", lambda: tmp_path / "audit.jsonl")
    spawns = []

    def fake_spawn(profile):
        spawns.append(profile)
        time.sleep(0.05)
        return 4321

    monkeypatch.setattr(module, "_spawn_profile_restart", fake_spawn)
    start = threading.Barrier(2)
    args = {
        "reason": "reload target configuration",
        "confirm": "restart gateway",
        "target_profile": "research",
    }

    def request_restart():
        start.wait()
        return json.loads(module._handle_request_gateway_restart(args))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: request_restart(), range(2)))

    assert sorted(result.get("status") or result.get("error") for result in results) == [
        "cooldown_active",
        "restart_scheduled",
    ]
    assert spawns == ["research"]


def test_failed_remote_spawn_releases_its_cooldown_reservation(monkeypatch, tmp_path):
    module = _load_plugin_module()
    monkeypatch.setattr(module, "_state_path", lambda: tmp_path / "restart-state.json")
    monkeypatch.setattr(
        module,
        "_plugin_config",
        lambda: {"allowed_target_profiles": ["research"], "cooldown_seconds": 60},
    )
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", lambda record: None)
    monkeypatch.setattr(module, "_audit_path", lambda: tmp_path / "audit.jsonl")
    attempts = []

    def fake_spawn(profile):
        attempts.append(profile)
        if len(attempts) == 1:
            raise OSError("transient spawn failure")
        return 4321

    monkeypatch.setattr(module, "_spawn_profile_restart", fake_spawn)
    args = {
        "reason": "reload target configuration",
        "confirm": "restart gateway",
        "target_profile": "research",
    }

    first = json.loads(module._handle_request_gateway_restart(args))
    second = json.loads(module._handle_request_gateway_restart(args))

    assert first["error"] == "profile_restart_spawn_failed"
    assert second["status"] == "restart_scheduled"
    assert attempts == ["research", "research"]


def test_legacy_cooldown_remains_conservative_until_scoped_state_exists(monkeypatch, tmp_path):
    module = _load_plugin_module()
    state_path = tmp_path / "restart-state.json"
    state_path.write_text('{"last_requested_at": 100.0}', encoding="utf-8")
    monkeypatch.setattr(module, "_state_path", lambda: state_path)

    assert module._read_last_restart_time("default", "ops") == 100.0
    assert module._read_last_restart_time("research", "ops") == 100.0


def test_restart_batch_validates_and_schedules_each_allowed_target(monkeypatch):
    module = _load_plugin_module()
    writes = []
    spawns = []
    monkeypatch.setattr(
        module,
        "_plugin_config",
        lambda: {"allowed_target_profiles": ["default", "research"]},
    )
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", lambda record: None)
    monkeypatch.setattr(module, "_read_last_restart_time", lambda target, source: 0.0)
    monkeypatch.setattr(module, "_write_last_restart_time", lambda target, now: writes.append(target))
    monkeypatch.setattr(module, "_spawn_profile_restart", lambda target: spawns.append(target) or 4321)

    result = json.loads(
        module._handle_request_gateway_restart(
            {
                "reason": "reload every authorized gateway",
                "confirm": "restart gateway",
                "target_profiles": ["ops", "default", "research"],
            }
        )
    )

    # The source profile is intentionally scheduled last; remote gateways get
    # their commands before this agent begins draining its own gateway.
    assert result["ok"] is False  # local runner is unavailable in this unit test
    assert result["status"] == "restart_batch_scheduled"
    assert result["target_profiles"] == ["default", "research", "ops"]
    assert spawns == ["default", "research"]
    assert writes == ["default", "research"]


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
