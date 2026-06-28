from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def isolated_profile_cron(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    profile_home = root / "profiles" / "writer"
    cron_dir = profile_home / "cron"
    output_dir = cron_dir / "output"
    output_dir.mkdir(parents=True)
    events_dir = root / "events" / "cron"

    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_CRON_EVENTS_ENABLED", "1")
    monkeypatch.setenv("HERMES_CRON_EVENTS_DIR", str(events_dir))

    import cron.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "HERMES_DIR", profile_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", output_dir)

    yield root, profile_home, events_dir


def _read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_cron_create_update_remove_publish_redacted_cross_profile_events(isolated_profile_cron):
    _root, _profile_home, events_dir = isolated_profile_cron

    from cron.jobs import create_job, remove_job, update_job

    job = create_job(
        prompt="Private prompt should not leave the owning profile",
        schedule="every 1h",
        name="Calendar-visible job",
        deliver="origin",
        origin={"platform": "telegram", "chat_id": "private-chat"},
        script="private-script.py",
    )
    update_job(job["id"], {"schedule": "every 2h"})
    assert remove_job(job["id"]) is True

    events = _read_jsonl(events_dir / "writer.jsonl")
    assert [event["event_type"] for event in events] == ["create", "update", "remove"]
    assert {event["source_profile"] for event in events} == {"writer"}
    assert all(event["schema_version"] == 1 for event in events)
    assert all(event["job_id"] == job["id"] for event in events)

    create_event = events[0]
    assert create_event["job"]["name"] == "Calendar-visible job"
    assert create_event["job"]["schedule_display"] == "every 60m"
    serialized = json.dumps(events)
    assert "Private prompt" not in serialized
    assert "private-chat" not in serialized
    assert "private-script.py" not in serialized


def test_complete_event_records_error_presence_without_error_text(isolated_profile_cron):
    _root, _profile_home, events_dir = isolated_profile_cron

    import cron.hooks as cron_hooks

    cron_hooks.emit(
        cron_hooks.COMPLETE,
        job={"id": "failed-job", "name": "Failed job", "last_error": "secret prompt leak"},
        success=False,
        duration_seconds=1.5,
        error="private provider payload with chat id 12345",
    )

    events = _read_jsonl(events_dir / "writer.jsonl")
    assert events[0]["event_type"] == "complete"
    assert events[0]["extra"] == {
        "success": False,
        "duration_seconds": 1.5,
        "error_present": True,
    }
    serialized = json.dumps(events)
    assert "private provider payload" not in serialized
    assert "secret prompt leak" not in serialized
    assert "last_error" not in serialized


def test_complete_event_records_error_presence_without_error_text(isolated_profile_cron):
    _root, _profile_home, events_dir = isolated_profile_cron

    import cron.hooks as cron_hooks

    cron_hooks.emit(
        cron_hooks.COMPLETE,
        job={"id": "failed-job", "name": "Failed job", "last_error": "secret prompt leak"},
        success=False,
        duration_seconds=1.5,
        error="private provider payload with chat id 12345",
    )

    events = _read_jsonl(events_dir / "writer.jsonl")
    assert events[0]["event_type"] == "complete"
    assert events[0]["extra"] == {
        "success": False,
        "duration_seconds": 1.5,
        "error_present": True,
    }
    serialized = json.dumps(events)
    assert "private provider payload" not in serialized
    assert "secret prompt leak" not in serialized
    assert "last_error" not in serialized


def test_dry_run_builds_event_without_writing(isolated_profile_cron):
    _root, _profile_home, events_dir = isolated_profile_cron

    from cron import event_bus

    record = event_bus.publish_cron_event(
        "update",
        job={
            "id": "job123",
            "name": "Dry run job",
            "prompt": "do not serialize",
            "schedule_display": "0 9 * * *",
        },
        source_profile="ops",
        dry_run=True,
    )

    assert record["source_profile"] == "ops"
    assert record["job_id"] == "job123"
    assert record["job"]["schedule_display"] == "0 9 * * *"
    assert "prompt" not in record["job"]
    assert not (events_dir / "ops.jsonl").exists()


def test_iter_events_reads_multiple_profile_streams(tmp_path, monkeypatch):
    events_dir = tmp_path / "events" / "cron"
    events_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_CRON_EVENTS_DIR", str(events_dir))

    from cron import event_bus

    event_bus.publish_cron_event("create", job={"id": "a", "name": "A"}, source_profile="ops")
    event_bus.publish_cron_event("create", job={"id": "b", "name": "B"}, source_profile="writer")

    all_events = list(event_bus.iter_events())
    assert [event["job_id"] for event in all_events] == ["a", "b"]

    writer_events = list(event_bus.iter_events(profiles=["writer"]))
    assert [event["job_id"] for event in writer_events] == ["b"]
