"""``post_cron_run``: observer fired once per completed cron run, after delivery."""
import threading
import time

import pytest

import cron.scheduler as s
from hermes_cli.plugins import PluginManager
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

JOB = {"id": "hooked", "name": "t", "deliver": "telegram"}


@pytest.fixture
def manager(monkeypatch):
    mgr = PluginManager()
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: mgr)
    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0.3)
    return mgr


@pytest.fixture
def pipeline(monkeypatch):
    state = {"success": True, "error": None, "final": "the answer", "events": []}

    monkeypatch.setattr(
        s, "run_job",
        lambda job, **_kw: (state["success"], "out", state["final"], state["error"]))
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(
        "cron.delivery_queue.enqueue",
        lambda *a, **kw: state["events"].append("deliver") or {"status": "pending"})
    monkeypatch.setattr(
        s, "mark_job_run", lambda jid, ok, *a, **kw: state["events"].append(("mark", ok)))
    return state


def test_fires_once_after_delivery_with_success_kwargs(manager, pipeline):
    seen = []

    def hook(**kw):
        pipeline["events"].append("hook")
        seen.append(kw)

    manager._hooks["post_cron_run"] = [hook]

    assert s.run_one_job(dict(JOB)) is True

    assert len(seen) == 1
    kw = seen[0]
    assert kw["job_id"] == "hooked"
    assert kw["execution_id"]
    assert kw["success"] is True
    assert kw["error"] is None
    assert kw["final_response"] == "the answer"
    assert str(kw["output_file"]) == "/tmp/hooked.txt"
    assert pipeline["events"].index("deliver") < pipeline["events"].index("hook")


def test_fires_once_for_failed_run_with_error(manager, pipeline):
    pipeline.update(success=False, error="model exploded", final="")
    seen = []
    manager._hooks["post_cron_run"] = [lambda **kw: seen.append(kw)]

    s.run_one_job(dict(JOB))

    assert len(seen) == 1
    assert seen[0]["success"] is False
    assert seen[0]["error"] == "model exploded"
    assert seen[0]["job_id"] == "hooked"


def test_raising_hook_does_not_break_the_run(manager, pipeline):
    def boom(**_kw):
        raise RuntimeError("plugin blew up")

    manager._hooks["post_cron_run"] = [boom]

    assert s.run_one_job(dict(JOB)) is True
    assert ("mark", True) in pipeline["events"]


def test_hanging_hook_is_bounded_by_hook_callback_timeout(manager, pipeline):
    release = threading.Event()
    manager._hooks["post_cron_run"] = [lambda **_kw: release.wait(10)]

    started = time.monotonic()
    try:
        assert s.run_one_job(dict(JOB)) is True
    finally:
        release.set()

    assert time.monotonic() - started < 5
    assert ("mark", True) in pipeline["events"]


def test_hook_runs_inside_the_jobs_profile_home(manager, pipeline, tmp_path):
    profile_home = tmp_path / "profile-b"
    profile_home.mkdir()
    homes = []
    manager._hooks["post_cron_run"] = [lambda **_kw: homes.append(get_hermes_home())]

    token = set_hermes_home_override(str(profile_home))
    try:
        s.run_one_job(dict(JOB))
    finally:
        reset_hermes_home_override(token)

    assert homes == [profile_home]


def test_empty_response_run_fires_once_as_failure(manager, pipeline):
    pipeline.update(final="   ")
    seen = []
    manager._hooks["post_cron_run"] = [lambda **kw: seen.append(kw)]

    s.run_one_job(dict(JOB))

    assert len(seen) == 1
    assert seen[0]["success"] is False
    assert seen[0]["error"]


def test_shutdown_interrupted_run_does_not_fire(manager, pipeline, monkeypatch):
    seen = []
    manager._hooks["post_cron_run"] = [lambda **kw: seen.append(kw)]
    monkeypatch.setattr(s, "_consume_interrupted_flag", lambda *a, **kw: True)

    s.run_one_job(dict(JOB))

    assert seen == []
