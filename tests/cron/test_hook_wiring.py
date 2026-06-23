"""Tests that cron mutations and run completion emit the right lifecycle hooks."""

import pytest

from cron import hooks
from cron.jobs import create_job, update_job, remove_job, mark_job_run
from cron import scheduler


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_hooks():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


def _record(event):
    events = []
    hooks.register_hook(event, lambda **kw: events.append(kw))
    return events


def test_create_job_emits_create(tmp_cron_dir):
    created = _record(hooks.CREATE)
    job = create_job(prompt="hi", schedule="every 10m", name="job-a")

    assert len(created) == 1
    assert created[0]["job"]["id"] == job["id"]
    assert created[0]["job"]["name"] == "job-a"


def test_update_job_emits_update(tmp_cron_dir):
    job = create_job(prompt="hi", schedule="every 10m", name="job-a")
    updated_events = _record(hooks.UPDATE)

    update_job(job["id"], {"name": "renamed"})

    assert len(updated_events) == 1
    assert updated_events[0]["job"]["name"] == "renamed"


def test_remove_job_emits_remove(tmp_cron_dir):
    job = create_job(prompt="hi", schedule="every 10m", name="job-a")
    removed = _record(hooks.REMOVE)

    remove_job(job["id"])

    assert len(removed) == 1
    assert removed[0]["job"]["id"] == job["id"]


def test_repeat_limit_autodelete_emits_remove(tmp_cron_dir):
    # One-shot job (repeat times=1) auto-deletes on completion.
    job = create_job(prompt="hi", schedule="30m", name="oneshot", repeat=1)
    removed = _record(hooks.REMOVE)

    mark_job_run(job["id"], success=True)

    assert len(removed) == 1
    assert removed[0]["job"]["id"] == job["id"]


def test_emit_complete_payload_and_log_fallback(monkeypatch, caplog):
    # No delivery targets -> notify falls back to logging.
    monkeypatch.setattr(scheduler, "_resolve_delivery_targets", lambda job: [])
    completed = _record(hooks.COMPLETE)

    job = {"id": "j1", "name": "job", "deliver": "local"}
    scheduler._emit_complete(job, success=True, duration_seconds=12.5, error=None)

    assert len(completed) == 1
    payload = completed[0]
    assert payload["job"]["id"] == "j1"
    assert payload["success"] is True
    assert payload["duration_seconds"] == 12.5
    assert payload["error"] is None
    assert callable(payload["notify"])

    # notify with no targets must not raise; it logs.
    import logging
    with caplog.at_level(logging.WARNING):
        payload["notify"]("hello warning", warn=True)
    assert "hello warning" in caplog.text


def test_emit_complete_notify_delivers_when_targets(monkeypatch):
    delivered = {}

    monkeypatch.setattr(scheduler, "_resolve_delivery_targets",
                        lambda job: [{"platform": "discord", "chat_id": "c"}])

    def fake_deliver(job, content, adapters=None, loop=None, raw_content=False):
        delivered["content"] = content
        delivered["raw"] = raw_content
        return None

    monkeypatch.setattr(scheduler, "_deliver_result", fake_deliver)
    completed = _record(hooks.COMPLETE)

    job = {"id": "j1", "name": "job", "deliver": "origin"}
    scheduler._emit_complete(job, success=True, duration_seconds=1.0, error=None)
    completed[0]["notify"]("a message")

    assert delivered["content"] == "a message"
    assert delivered["raw"] is True
