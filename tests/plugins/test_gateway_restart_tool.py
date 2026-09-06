"""FC-36 real gateway request_restart path; teardown effects are isolated."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def load_plugin():
    path = Path(__file__).resolve().parents[2] / "plugins/gateway-restart-tool/__init__.py"
    spec = importlib.util.spec_from_file_location("restart_plugin_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def configured(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: [gateway-restart-tool]\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


@pytest.mark.asyncio
async def test_worker_thread_requests_real_gateway_drain_before_stop(configured, monkeypatch):
    import gateway.run as gateway_run
    from gateway.session_context import set_session_vars, clear_session_vars
    module = load_plugin()
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._gateway_profile_home = configured
    runner._gateway_loop = asyncio.get_running_loop()
    runner._restart_task_started = False
    runner._restart_requested = False
    runner._draining = False
    runner._running_agents = {"caller": SimpleNamespace()}
    runner._active_cron_job_count = lambda: 0
    runner._active_api_run_count = lambda: 0
    runner._wedged_agent_count = lambda: 0
    runner._restart_after_turn_timeout = 10
    runner._update_runtime_status = lambda *args: None
    runner.stop = AsyncMock()
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    monkeypatch.setattr(module, "_restart_modes", lambda: (False, True))
    tokens = set_session_vars(platform="telegram", session_key="caller")
    try:
        result = json.loads(await asyncio.wait_for(asyncio.to_thread(
            module._handle_request_gateway_restart,
            {"reason": "test reload", "confirm": "restart gateway"},
        ), timeout=5))
        assert result["ok"] and result["status"] == "restart_draining"
        assert runner._draining
        runner.stop.assert_not_awaited()
        audit = [json.loads(line) for line in module._audit_path().read_text().splitlines()]
        assert any(row["decision"] == "reserved" for row in audit)
        assert audit[-1]["decision"] == "scheduled"
        runner._running_agents.clear()  # requesting turn delivered its final
        await asyncio.wait_for(runner._restart_task, timeout=3)
        runner.stop.assert_awaited_once_with(restart=True, detached_restart=False, service_restart=True)
    finally:
        clear_session_vars(tokens)
        task = getattr(runner, "_restart_task", None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_outside_gateway_session_cannot_restart(configured):
    module = load_plugin()
    result = json.loads(module._handle_request_gateway_restart({"reason":"test", "confirm":"restart gateway"}))
    assert not result["ok"]


def test_legacy_cooldown_state_is_read_without_migration(configured, monkeypatch):
    module=load_plugin()
    monkeypatch.setattr(module,"_resolve_runner",lambda:None)
    module._state_path().write_text(json.dumps({"last_requested_at_by_profile":{"ang":1000,"default":1100}}))
    assert module._reserve_restart(1150,300)==250
    assert "last_requested_at_by_profile" in json.loads(module._state_path().read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["dry_run", "disabled", "wrong_confirm", "no_reason", "operator_drain", "audit_failure", "cooldown"])
async def test_restart_guards_leave_gateway_running(configured, monkeypatch, case):
    import gateway.run as gateway_run
    from gateway.session_context import set_session_vars, clear_session_vars
    module = load_plugin()
    calls = []
    runner = SimpleNamespace(
        _gateway_profile_home=configured, _gateway_loop=asyncio.get_running_loop(),
        _restart_requested=False, _draining=case == "operator_drain",
        _running_agents={"caller": object()},
        _running_agent_count=lambda: 1, _active_work_count=lambda: 1,
        request_restart=lambda **kwargs: calls.append(kwargs) or True,
    )
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    tokens = set_session_vars(platform="telegram", session_key="caller")
    args = {"reason": "test", "confirm": "restart gateway"}
    if case == "dry_run": args["dry_run"] = True
    if case == "disabled": (configured / "config.yaml").write_text("plugins:\n  enabled: []\n")
    if case == "wrong_confirm": args["confirm"] = "yes"
    if case == "no_reason": args["reason"] = ""
    if case == "audit_failure":
        def fail(record): raise OSError("test audit unavailable")
        monkeypatch.setattr(module, "_append_audit", fail)
    if case == "cooldown": module._reserve_restart(__import__('time').time(), 300)
    try:
        result = json.loads(await asyncio.to_thread(module._handle_request_gateway_restart, args))
        assert result["ok"] is (case == "dry_run")
        assert calls == []
        if case in {"audit_failure", "dry_run"}:
            assert not module._state_path().exists()
    finally:
        clear_session_vars(tokens)


@pytest.mark.asyncio
async def test_concurrent_profiles_share_owner_cooldown(configured, monkeypatch):
    import gateway.run as gateway_run
    from gateway.session_context import set_session_vars, clear_session_vars
    module = load_plugin()
    calls=[]
    runner = SimpleNamespace(_gateway_profile_home=configured, _gateway_loop=asyncio.get_running_loop(),
        _restart_requested=False, _draining=False, _running_agents={"caller": object()},
        _running_agent_count=lambda:1, _active_work_count=lambda:1,
        request_restart=lambda **kw: calls.append(kw) or True)
    monkeypatch.setattr(gateway_run,"_gateway_runner_ref",lambda:runner)
    tokens=set_session_vars(platform="telegram",session_key="caller")
    try:
        args={"reason":"concurrent", "confirm":"restart gateway"}
        results=await asyncio.wait_for(asyncio.gather(*[
            asyncio.to_thread(module._handle_request_gateway_restart,args) for _ in range(3)
        ]),timeout=8)
        assert len(calls)==1
        assert sum(json.loads(result)["ok"] for result in results)==1
    finally:clear_session_vars(tokens)


import multiprocessing
import time

def _plugin_path():
    return Path(__file__).resolve().parents[2] / "plugins/gateway-restart-tool/__init__.py"

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
