"""Fresh release evidence is distinct from answering a status request."""
import asyncio
import os
import time

import pytest

from gateway.config import Platform
from gateway.control_socket import GatewayControlServer, query_gateway_control
from gateway.run import GatewayRunner
from gateway.status import activity_is_fresh_zero, write_runtime_status


def _runner(monkeypatch):
    from cron import scheduler
    monkeypatch.setattr(scheduler, "get_running_job_keys", lambda: set())
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner.adapters = {}
    runner._running = True
    runner._draining = False
    runner._external_drain_active = True
    return runner


def test_real_control_status_samples_current_work_not_persisted_count(tmp_path, monkeypatch):
    runner = _runner(monkeypatch)
    write_runtime_status(gateway_state="running", active_agents=9)

    async def scenario():
        server = GatewayControlServer(tmp_path, verb_handlers={"status": runner._live_control_status})
        assert await server.start()
        try:
            loop = asyncio.get_running_loop()
            runner._running_agents["legitimate-turn"] = object()
            busy = await loop.run_in_executor(None, lambda: query_gateway_control(tmp_path, "status"))
            assert busy["active_agents"] == 1
            assert busy["activity_state"] == "fresh"
            assert not activity_is_fresh_zero(busy, expected_pid=os.getpid())
            runner._running_agents.pop("legitimate-turn")
            idle = await loop.run_in_executor(None, lambda: query_gateway_control(tmp_path, "status"))
            assert idle["active_agents"] == 0
            assert activity_is_fresh_zero(idle, expected_pid=os.getpid())
            assert idle["activity_sampled_at"] >= busy["activity_sampled_at"]
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_sampling_error_is_unknown_not_zero(tmp_path, monkeypatch):
    runner = _runner(monkeypatch)

    class BrokenAPI:
        def active_agent_work_count(self):
            raise RuntimeError("cannot sample")

    runner.adapters[Platform.API_SERVER] = BrokenAPI()
    evidence = runner._live_control_status()
    assert evidence["active_agents"] is None
    assert evidence["activity_state"] == "unknown"
    assert not activity_is_fresh_zero(evidence, expected_pid=os.getpid())


@pytest.mark.parametrize("change", [
    {"activity_sampled_at": time.time() - 600},
    {"activity_sampled_at": time.time() + 600},
    {"activity_sampled_at": None},
    {"activity_writer_pid": -1},
    {"active_agents": "0"},
    {"active_agents": False},
    {"activity_state": "unknown"},
    {"gateway_state": "running"},
])
def test_stale_or_unbound_evidence_never_permits_stop(change):
    evidence = {"activity_sampled_at": time.time(), "activity_writer_pid": os.getpid(),
                "active_agents": 0, "activity_state": "fresh", "gateway_state": "draining"}
    evidence.update(change)
    assert not activity_is_fresh_zero(evidence, expected_pid=os.getpid())


def test_persisted_status_without_activity_sample_is_not_stop_evidence():
    assert not activity_is_fresh_zero({"answered_at": time.time(), "updated_at": time.time(),
                                      "active_agents": 0}, expected_pid=os.getpid())


@pytest.mark.parametrize("failure", ["counter", "task"])
def test_review_real_api_sampling_error_never_becomes_fresh_zero(monkeypatch, failure):
    from gateway.platforms.api_server import APIServerAdapter
    runner = _runner(monkeypatch)
    adapter = object.__new__(APIServerAdapter)
    adapter._pending_agent_requests = 0
    adapter._inflight_agent_runs = 0
    adapter._active_run_tasks = {}
    if failure == "counter":
        adapter._inflight_agent_runs = "not a counter"
    else:
        class BrokenTask:
            def done(self):
                raise RuntimeError("cannot inspect task")
        adapter._active_run_tasks["broken"] = BrokenTask()
    # Existing best-effort consumers retain their historical fallback.
    assert adapter.active_agent_work_count() == 0
    runner.adapters[Platform.API_SERVER] = adapter
    evidence = runner._live_control_status()
    assert evidence["activity_state"] == "unknown"
    assert evidence["active_agents"] is None
    assert not activity_is_fresh_zero(evidence, expected_pid=os.getpid())


@pytest.mark.parametrize("pending", [0, 2])
def test_review_real_api_successful_sample_is_fresh(monkeypatch, pending):
    from gateway.platforms.api_server import APIServerAdapter
    runner = _runner(monkeypatch)
    adapter = object.__new__(APIServerAdapter)
    adapter._pending_agent_requests = pending
    adapter._inflight_agent_runs = 0
    adapter._active_run_tasks = {}
    runner.adapters[Platform.API_SERVER] = adapter
    evidence = runner._live_control_status()
    assert evidence["activity_state"] == "fresh"
    assert evidence["active_agents"] == pending
    assert activity_is_fresh_zero(evidence, expected_pid=os.getpid()) == (pending == 0)


def test_control_status_exposes_verified_drain_evidence(monkeypatch):
    runner = _runner(monkeypatch)
    runner._running_agents["actual-turn"] = object()
    assert runner._live_control_status()["activity_zero_verified"] is False
    runner._running_agents.clear()
    assert runner._live_control_status()["activity_zero_verified"] is True
    runner._external_drain_active = False
    assert runner._live_control_status()["activity_zero_verified"] is False
