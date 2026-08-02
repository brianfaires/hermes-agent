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


def _plugin_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "gateway-restart-tool"
        / "__init__.py"
    )


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "gateway_restart_tool_plugin", _plugin_path()
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runner(*, active_agents: int = 2):
    return types.SimpleNamespace(
        _restart_requested=False,
        _draining=False,
        _running_agent_count=lambda: active_agents,
    )


def _hold_restart_state_lock(plugin_path, state_path, acquired, release):
    spec = importlib.util.spec_from_file_location(
        "gateway_restart_tool_lock_holder", plugin_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "_state_path", lambda: Path(state_path))
    with module._restart_state_lock():
        acquired.set()
        release.wait(5)


def _wait_for_restart_state_lock(plugin_path, state_path, acquired):
    spec = importlib.util.spec_from_file_location(
        "gateway_restart_tool_lock_waiter", plugin_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "_state_path", lambda: Path(state_path))
    with module._restart_state_lock():
        acquired.set()


def _reserve_with_delayed_write(
    plugin_path, state_path, entered_write, release_write, results
):
    spec = importlib.util.spec_from_file_location(
        "gateway_restart_tool_reserver_a", plugin_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "_state_path", lambda: Path(state_path))
    original_write = module._write_last_restart_time

    def delayed_write(now):
        entered_write.set()
        release_write.wait(5)
        original_write(now)

    setattr(module, "_write_last_restart_time", delayed_write)
    results.put(module._reserve_restart(1000.0, 60))


def _reserve_and_signal(plugin_path, state_path, ready, done, results):
    spec = importlib.util.spec_from_file_location(
        "gateway_restart_tool_reserver_b", plugin_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "_state_path", lambda: Path(state_path))
    ready.set()
    results.put(module._reserve_restart(1000.0, 60))
    done.set()


def test_schema_is_one_process_wide_tool():
    module = _load_plugin_module()

    schema = module.REQUEST_GATEWAY_RESTART_SCHEMA

    assert schema["description"] == "Restart the shared Hermes gateway for all profiles."
    assert set(schema["parameters"]["properties"]) == {
        "reason",
        "confirm",
        "dry_run",
    }
    assert schema["parameters"]["additionalProperties"] is False


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


def test_dry_run_reports_process_scope_without_restarting(monkeypatch, tmp_path):
    module = _load_plugin_module()
    runner = _runner()
    monkeypatch.setattr(module, "_plugin_config", lambda: {})
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", lambda record: None)
    monkeypatch.setattr(module, "_audit_path", lambda: tmp_path / "audit.jsonl")
    monkeypatch.setattr(module, "_resolve_runner", lambda: runner)
    monkeypatch.setattr(
        module,
        "_schedule_restart",
        lambda *_args: pytest.fail("dry run scheduled a restart"),
    )

    result = json.loads(
        module._handle_request_gateway_restart(
            {
                "reason": "reload configuration",
                "confirm": "restart gateway",
                "dry_run": True,
            }
        )
    )

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["restart_scope"] == "all_profiles"
    assert result["profile"] == "ops"


def test_real_restart_requires_live_runner(monkeypatch):
    module = _load_plugin_module()
    monkeypatch.setattr(module, "_plugin_config", lambda: {})
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", lambda record: None)
    monkeypatch.setattr(module, "_resolve_runner", lambda: None)

    result = json.loads(
        module._handle_request_gateway_restart(
            {"reason": "reload configuration", "confirm": "restart gateway"}
        )
    )

    assert result["error"] == "gateway_runner_unavailable"


@pytest.mark.parametrize(
    ("args", "expected_error"),
    [
        ({"reason": "", "confirm": "restart gateway"}, "missing_reason"),
        ({"reason": "reload configuration", "confirm": "yes"}, "confirmation_required"),
        (
            {"reason": "reload configuration", "confirm": "RESTART GATEWAY"},
            "confirmation_required",
        ),
        (
            {"reason": "reload configuration", "confirm": " restart gateway "},
            "confirmation_required",
        ),
    ],
)
def test_restart_rejects_missing_reason_or_confirmation(
    monkeypatch, args, expected_error
):
    module = _load_plugin_module()
    scheduled = []
    monkeypatch.setattr(module, "_plugin_config", lambda: {})
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", lambda record: None)
    monkeypatch.setattr(module, "_resolve_runner", lambda: _runner())
    monkeypatch.setattr(
        module,
        "_schedule_restart",
        lambda *_args: scheduled.append(True) or True,
    )

    result = json.loads(module._handle_request_gateway_restart(args))

    assert result["error"] == expected_error
    assert scheduled == []


def test_restart_schedules_one_shared_gateway_restart(monkeypatch, tmp_path):
    module = _load_plugin_module()
    runner = _runner(active_agents=0)
    scheduled = []
    monkeypatch.setattr(module, "_plugin_config", lambda: {"cooldown_seconds": 0})
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", lambda record: None)
    monkeypatch.setattr(module, "_audit_path", lambda: tmp_path / "audit.jsonl")
    monkeypatch.setattr(module, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(module, "_resolve_runner", lambda: runner)
    monkeypatch.setattr(
        module,
        "_schedule_restart",
        lambda actual_runner, delay: scheduled.append((actual_runner, delay)) or True,
    )

    result = json.loads(
        module._handle_request_gateway_restart(
            {"reason": "reload configuration", "confirm": "restart gateway"}
        )
    )

    assert result["ok"] is True
    assert result["status"] == "restart_scheduled"
    assert result["restart_scope"] == "all_profiles"
    assert result["profile"] == "ops"
    assert len(scheduled) == 1
    assert scheduled[0][0] is runner


@pytest.mark.parametrize("active_agents", [1, 3])
def test_restart_denies_when_live_runner_has_active_agents(
    monkeypatch, tmp_path, active_agents
):
    module = _load_plugin_module()
    runner = _runner(active_agents=active_agents)
    audit_records = []
    monkeypatch.setattr(module, "_plugin_config", lambda: {"cooldown_seconds": 60})
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", audit_records.append)
    monkeypatch.setattr(module, "_audit_path", lambda: tmp_path / "audit.jsonl")
    monkeypatch.setattr(module, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(module, "_resolve_runner", lambda: runner)
    monkeypatch.setattr(
        module,
        "_reserve_restart",
        lambda *_args: pytest.fail("active-agent denial reserved cooldown"),
    )
    monkeypatch.setattr(
        module,
        "_schedule_restart",
        lambda *_args: pytest.fail("active-agent denial scheduled a restart"),
    )

    result = json.loads(
        module._handle_request_gateway_restart(
            {"reason": "reload configuration", "confirm": "restart gateway"}
        )
    )

    assert result["ok"] is False
    assert result["error"] == "active_agents_present"
    assert result["active_agents"] == active_agents
    assert (
        result["retry"]
        == "Retry only after active_agents reaches zero and no agents are running."
    )
    assert audit_records == [
        {
            "ts": audit_records[0]["ts"],
            "profile": "ops",
            "reason": "reload configuration",
            "dry_run": False,
            "restart_scope": "all_profiles",
            "decision": "deny",
            "error": "active_agents_present",
            "active_agents": active_agents,
        }
    ]


def test_restart_state_lock_serializes_processes(tmp_path):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("cross-process lock test requires fork")
    ctx = multiprocessing.get_context("fork")
    state_path = tmp_path / "restart-state.json"
    holder_acquired = ctx.Event()
    release_holder = ctx.Event()
    waiter_acquired = ctx.Event()
    process_type = getattr(ctx, "Process")
    holder = process_type(
        target=_hold_restart_state_lock,
        args=(str(_plugin_path()), str(state_path), holder_acquired, release_holder),
    )
    waiter = process_type(
        target=_wait_for_restart_state_lock,
        args=(str(_plugin_path()), str(state_path), waiter_acquired),
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
    state_path = tmp_path / "restart-state.json"
    entered_write = ctx.Event()
    release_write = ctx.Event()
    contender_ready = ctx.Event()
    contender_done = ctx.Event()
    results = ctx.Queue()
    process_type = getattr(ctx, "Process")
    holder = process_type(
        target=_reserve_with_delayed_write,
        args=(
            str(_plugin_path()),
            str(state_path),
            entered_write,
            release_write,
            results,
        ),
    )
    contender = process_type(
        target=_reserve_and_signal,
        args=(
            str(_plugin_path()),
            str(state_path),
            contender_ready,
            contender_done,
            results,
        ),
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


def test_concurrent_requests_reserve_process_cooldown_once(monkeypatch, tmp_path):
    module = _load_plugin_module()
    runner = _runner(active_agents=0)
    scheduled = []
    monkeypatch.setattr(module, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(module, "_plugin_config", lambda: {"cooldown_seconds": 60})
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", lambda record: None)
    monkeypatch.setattr(module, "_audit_path", lambda: tmp_path / "audit.jsonl")
    monkeypatch.setattr(module, "_resolve_runner", lambda: runner)

    def schedule(*_args):
        scheduled.append(True)
        time.sleep(0.05)
        return True

    monkeypatch.setattr(module, "_schedule_restart", schedule)
    barrier = threading.Barrier(2)
    args = {"reason": "reload configuration", "confirm": "restart gateway"}

    def request_restart():
        barrier.wait()
        return json.loads(module._handle_request_gateway_restart(args))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: request_restart(), range(2)))

    assert sorted(result.get("status") or result.get("error") for result in results) == [
        "cooldown_active",
        "restart_scheduled",
    ]
    assert scheduled == [True]


def test_failed_schedule_releases_cooldown_reservation(monkeypatch, tmp_path):
    module = _load_plugin_module()
    runner = _runner(active_agents=0)
    monkeypatch.setattr(module, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(module, "_plugin_config", lambda: {"cooldown_seconds": 60})
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", lambda record: None)
    monkeypatch.setattr(module, "_audit_path", lambda: tmp_path / "audit.jsonl")
    monkeypatch.setattr(module, "_resolve_runner", lambda: runner)
    outcomes = iter([False, True])
    monkeypatch.setattr(module, "_schedule_restart", lambda *_args: next(outcomes))
    args = {"reason": "reload configuration", "confirm": "restart gateway"}

    first = json.loads(module._handle_request_gateway_restart(args))
    second = json.loads(module._handle_request_gateway_restart(args))

    assert first["error"] == "schedule_failed"
    assert second["status"] == "restart_scheduled"
