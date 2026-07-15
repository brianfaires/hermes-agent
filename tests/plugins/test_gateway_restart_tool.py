from __future__ import annotations

import builtins
import importlib.util
import json
import multiprocessing
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def _hold_restart_state_lock(plugin_path, state_path, acquired, release):
    spec = importlib.util.spec_from_file_location("gateway_restart_tool_lock_holder", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "_state_path", lambda: Path(state_path))
    with module._restart_state_lock():
        acquired.set()
        release.wait(5)


def _wait_for_restart_state_lock(plugin_path, state_path, acquired):
    spec = importlib.util.spec_from_file_location("gateway_restart_tool_lock_waiter", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "_state_path", lambda: Path(state_path))
    with module._restart_state_lock():
        acquired.set()


def _reserve_with_delayed_write(plugin_path, state_path, entered_write, release_write, results):
    spec = importlib.util.spec_from_file_location("gateway_restart_tool_reserver_a", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "_state_path", lambda: Path(state_path))
    original_write = module._write_last_restart_time

    def delayed_write(target_profile, now):
        entered_write.set()
        release_write.wait(5)
        original_write(target_profile, now)

    setattr(module, "_write_last_restart_time", delayed_write)
    results.put(module._reserve_restart("research", "ops", 1000.0, 60))


def _reserve_and_signal(plugin_path, state_path, ready, done, results):
    spec = importlib.util.spec_from_file_location("gateway_restart_tool_reserver_b", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "_state_path", lambda: Path(state_path))
    ready.set()
    results.put(module._reserve_restart("research", "ops", 1000.0, 60))
    done.set()


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


@pytest.mark.parametrize(
    ("marker", "value"),
    [
        ("INVOCATION_ID", "systemd-unit"),
        ("HERMES_S6_SUPERVISED_CHILD", "1"),
        ("XPC_SERVICE_NAME", "com.hermes.gateway"),
        ("HERMES_GATEWAY_EXTERNAL_SUPERVISOR", "true"),
    ],
)
def test_restart_modes_reuse_all_gateway_supervisor_markers(
    monkeypatch, marker, value
):
    module = _load_plugin_module()
    for name in (
        "INVOCATION_ID",
        "HERMES_S6_SUPERVISED_CHILD",
        "XPC_SERVICE_NAME",
        "HERMES_GATEWAY_EXTERNAL_SUPERVISOR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(module.os.path, "exists", lambda _path: False)
    monkeypatch.setenv(marker, value)

    assert module._restart_modes() == (False, True)


def test_restart_modes_use_detached_restart_without_supervisor_or_container(monkeypatch):
    module = _load_plugin_module()
    for name in (
        "INVOCATION_ID",
        "HERMES_S6_SUPERVISED_CHILD",
        "XPC_SERVICE_NAME",
        "HERMES_GATEWAY_EXTERNAL_SUPERVISOR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(module.os.path, "exists", lambda _path: False)

    assert module._restart_modes() == (True, False)


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


def test_restart_state_lock_serializes_processes(tmp_path):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("cross-process lock test requires fork")
    ctx = multiprocessing.get_context("fork")
    plugin_path = Path(__file__).resolve().parents[2] / "plugins" / "gateway-restart-tool" / "__init__.py"
    state_path = tmp_path / "restart-state.json"
    holder_acquired = ctx.Event()
    release_holder = ctx.Event()
    waiter_acquired = ctx.Event()
    holder = ctx.Process(
        target=_hold_restart_state_lock,
        args=(str(plugin_path), str(state_path), holder_acquired, release_holder),
    )
    waiter = ctx.Process(
        target=_wait_for_restart_state_lock,
        args=(str(plugin_path), str(state_path), waiter_acquired),
    )

    holder.start()
    assert holder_acquired.wait(5)
    waiter.start()
    assert not waiter_acquired.wait(0.2)
    release_holder.set()
    assert waiter_acquired.wait(5)
    holder.join(5)
    waiter.join(5)

    assert holder.exitcode == 0
    assert waiter.exitcode == 0


def test_restart_reservation_is_atomic_across_processes(tmp_path):
    method = "spawn" if "spawn" in multiprocessing.get_all_start_methods() else "fork"
    ctx = multiprocessing.get_context(method)
    plugin_path = Path(__file__).resolve().parents[2] / "plugins" / "gateway-restart-tool" / "__init__.py"
    state_path = tmp_path / "restart-state.json"
    entered_write = ctx.Event()
    release_write = ctx.Event()
    contender_ready = ctx.Event()
    contender_done = ctx.Event()
    results = ctx.Queue()
    process_type = getattr(ctx, "Process")
    holder = process_type(
        target=_reserve_with_delayed_write,
        args=(str(plugin_path), str(state_path), entered_write, release_write, results),
    )
    contender = process_type(
        target=_reserve_and_signal,
        args=(str(plugin_path), str(state_path), contender_ready, contender_done, results),
    )

    holder.start()
    assert entered_write.wait(5)
    contender.start()
    assert contender_ready.wait(5)
    assert not contender_done.wait(0.2)
    release_write.set()
    holder.join(5)
    contender.join(5)

    assert holder.exitcode == 0
    assert contender.exitcode == 0
    assert sorted([results.get(timeout=1), results.get(timeout=1)]) == [0, 60]


def test_restart_state_lock_uses_windows_byte_range_lock(monkeypatch, tmp_path):
    module = _load_plugin_module()
    monkeypatch.setattr(module, "_state_path", lambda: tmp_path / "restart-state.json")
    calls = []
    fake_msvcrt = types.SimpleNamespace(
        LK_LOCK=1,
        LK_UNLCK=2,
        locking=lambda fd, mode, size: calls.append((mode, size)),
    )
    real_import = builtins.__import__

    def platform_import(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError
        if name == "msvcrt":
            return fake_msvcrt
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", platform_import)
    with module._restart_state_lock():
        pass

    assert calls == [(fake_msvcrt.LK_LOCK, 1), (fake_msvcrt.LK_UNLCK, 1)]


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


def test_failed_local_schedule_releases_its_cooldown_reservation(monkeypatch, tmp_path):
    module = _load_plugin_module()
    monkeypatch.setattr(module, "_state_path", lambda: tmp_path / "restart-state.json")
    monkeypatch.setattr(module, "_plugin_config", lambda: {"cooldown_seconds": 60})
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", lambda record: None)
    monkeypatch.setattr(module, "_audit_path", lambda: tmp_path / "audit.jsonl")
    runner = type("Runner", (), {"_restart_requested": False, "_draining": False})()
    monkeypatch.setattr(module, "_resolve_runner", lambda: runner)
    outcomes = iter([False, True])
    monkeypatch.setattr(module, "_schedule_restart", lambda *args: next(outcomes))
    args = {"reason": "reload configuration", "confirm": "restart gateway"}

    first = json.loads(module._handle_request_gateway_restart(args))
    second = json.loads(module._handle_request_gateway_restart(args))

    assert first["error"] == "schedule_failed"
    assert second["status"] == "restart_scheduled"


def test_local_schedule_exception_releases_its_cooldown_reservation(monkeypatch, tmp_path):
    module = _load_plugin_module()
    monkeypatch.setattr(module, "_state_path", lambda: tmp_path / "restart-state.json")
    monkeypatch.setattr(module, "_plugin_config", lambda: {"cooldown_seconds": 60})
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", lambda record: None)
    monkeypatch.setattr(module, "_audit_path", lambda: tmp_path / "audit.jsonl")
    runner = type("Runner", (), {"_restart_requested": False, "_draining": False})()
    monkeypatch.setattr(module, "_resolve_runner", lambda: runner)
    attempts = 0

    def schedule(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("event loop unavailable")
        return True

    monkeypatch.setattr(module, "_schedule_restart", schedule)
    args = {"reason": "reload configuration", "confirm": "restart gateway"}

    first = json.loads(module._handle_request_gateway_restart(args))
    second = json.loads(module._handle_request_gateway_restart(args))

    assert first == {
        "ok": False,
        "error": "schedule_failed",
        "detail": "event loop unavailable",
    }
    assert second["status"] == "restart_scheduled"


def test_legacy_cooldown_remains_conservative_until_scoped_state_exists(monkeypatch, tmp_path):
    module = _load_plugin_module()
    state_path = tmp_path / "restart-state.json"
    state_path.write_text('{"last_requested_at": 100.0}', encoding="utf-8")
    monkeypatch.setattr(module, "_state_path", lambda: state_path)

    assert module._read_last_restart_time("default", "ops") == 100.0
    assert module._read_last_restart_time("research", "ops") == 100.0


def test_restart_batch_validates_and_schedules_each_allowed_target(monkeypatch, tmp_path):
    module = _load_plugin_module()
    writes = []
    spawns = []
    monkeypatch.setattr(module, "_state_path", lambda: tmp_path / "restart-state.json")
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
