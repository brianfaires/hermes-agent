from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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


def _read_events(events_dir: Path, profile: str = "writer"):
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted((events_dir / "pending" / profile).glob("*.json"))]


def test_cron_create_update_remove_publish_redacted_atomic_events(isolated_profile_cron):
    _root, _profile_home, events_dir = isolated_profile_cron
    from cron.jobs import create_job, remove_job, update_job
    job = create_job(prompt="Private prompt should not leave the owning profile", schedule="every 1h", name="Calendar-visible job", deliver="origin", origin={"platform": "telegram", "chat_id": "private-chat"}, script="private-script.py")
    update_job(job["id"], {"schedule": "every 2h"})
    assert remove_job(job["id"]) is True
    events = _read_events(events_dir)
    assert [event["event_type"] for event in events] == ["create", "update", "remove"]
    assert {event["source_profile"] for event in events} == {"writer"}
    assert all(event["schema_version"] == 1 for event in events)
    assert all(event["job_id"] == job["id"] for event in events)
    assert len({event["event_id"] for event in events}) == 3
    assert events[0]["job"]["name"] == "Calendar-visible job"
    assert events[0]["job"]["schedule_display"] == "every 60m"
    serialized = json.dumps(events)
    assert "Private prompt" not in serialized
    assert "private-chat" not in serialized
    assert "private-script.py" not in serialized
    assert not list(events_dir.rglob("*.tmp"))


def test_generated_job_name_does_not_publish_prompt_text(isolated_profile_cron):
    _root, _profile_home, events_dir = isolated_profile_cron
    from cron.jobs import create_job

    job = create_job(
        prompt="TOP SECRET customer acquisition details beyond production scope",
        schedule="every 1h",
        deliver="local",
    )
    event = _read_events(events_dir)[0]

    assert job["name"].startswith("TOP SECRET")
    assert "name" not in event["job"]
    assert "TOP SECRET" not in json.dumps(event)


def test_explicit_name_added_by_update_is_publishable(isolated_profile_cron):
    _root, _profile_home, events_dir = isolated_profile_cron
    from cron.jobs import create_job, update_job

    job = create_job(
        prompt="private generated fallback",
        schedule="every 1h",
        deliver="local",
    )
    update_job(job["id"], {"name": "Operator supplied display name"})
    events = _read_events(events_dir)

    assert "name" not in events[0]["job"]
    assert events[1]["job"]["name"] == "Operator supplied display name"


def test_name_provenance_marker_cannot_be_set_by_update(isolated_profile_cron):
    from cron.jobs import create_job, update_job

    job = create_job(
        prompt="private generated fallback",
        schedule="every 1h",
        deliver="local",
    )

    with pytest.raises(ValueError, match="name_is_explicit"):
        update_job(job["id"], {"name_is_explicit": True})


def test_complete_event_records_error_presence_without_error_text(isolated_profile_cron):
    _root, _profile_home, events_dir = isolated_profile_cron
    import cron.hooks as cron_hooks
    cron_hooks.emit(cron_hooks.COMPLETE, job={"id": "failed-job", "name": "Failed job", "last_error": "secret prompt leak", "prompt_path": "/private/prompt.md", "origin": {"chat_id": "private-chat"}, "deliver": "telegram:private-chat"}, success=False, duration_seconds=1.5, error="private provider payload with chat id 12345")
    events = _read_events(events_dir)
    assert events[0]["event_type"] == "complete"
    assert events[0]["extra"] == {"success": False, "duration_seconds": 1.5, "error_present": True}
    serialized = json.dumps(events)
    assert "private provider payload" not in serialized
    assert "secret prompt leak" not in serialized
    assert "last_error" not in serialized
    assert "/private/prompt.md" not in serialized
    assert "private-chat" not in serialized


def test_event_builder_rejects_free_form_job_and_extra_fields(isolated_profile_cron):
    from cron import event_bus

    record = event_bus.build_cron_event(
        "update",
        job={
            "id": "job-private",
            "name": "Visible name",
            "paused_reason": "private customer and incident details",
        },
        source_profile="ops",
        extra={
            "success": False,
            "duration_seconds": 1.5,
            "error_present": True,
            "private_payload": "must not cross profiles",
        },
    )

    assert "paused_reason" not in record["job"]
    assert record["extra"] == {
        "success": False,
        "duration_seconds": 1.5,
        "error_present": True,
    }


def test_nested_allowlisted_values_cannot_cross_profile_boundary(
    isolated_profile_cron,
):
    _root, _profile_home, events_dir = isolated_profile_cron
    from cron.jobs import create_job, update_job

    secret = "TOP-SECRET-NESTED-PAYLOAD"
    job = create_job(prompt="safe owner-only prompt", schedule="every 1h")
    with pytest.raises(ValueError, match="enabled_toolsets must be a list"):
        update_job(job["id"], {"enabled_toolsets": {"web": secret}})
    with pytest.raises(ValueError, match="cron job name must be a string"):
        update_job(job["id"], {"name": {"display": secret}})

    update_job(
        job["id"],
        {
            "schedule": {
                "kind": "interval",
                "minutes": 15,
                "display": secret,
                "secret_prompt": secret,
                "origin": {"chat_id": secret},
            },
            "repeat": {"note": secret},
        },
    )

    event = _read_events(events_dir)[-1]
    serialized = json.dumps(event)
    assert secret not in serialized
    assert event["job"]["schedule"] == {"kind": "interval", "minutes": 15}
    assert event["job"]["schedule_display"] == "every 15m"
    assert "repeat" not in event["job"]
    assert "enabled_toolsets" not in event["job"]

    with pytest.raises(ValueError, match="skill names must be plain strings"):
        update_job(
            job["id"],
            {
                "skills": [
                    {
                        "name": secret,
                        "prompt": secret,
                        "origin": {"chat_id": secret},
                    }
                ]
            },
        )
    with pytest.raises(ValueError, match="toolset names must be plain strings"):
        create_job(
            prompt="safe",
            schedule="every 1h",
            enabled_toolsets=[{"web": secret}],  # type: ignore[list-item]
        )
    with pytest.raises(ValueError, match="cron job name must be a string"):
        create_job(
            prompt="safe",
            schedule="every 1h",
            name={"display": secret},  # type: ignore[arg-type]
        )

    # Even hand-edited stored fields that look like nested payloads are dropped.
    from cron import event_bus

    record = event_bus.build_cron_event(
        "update",
        job={
            "id": job["id"],
            "skill": "{'name': '%s', 'prompt': '%s'}" % (secret, secret),
            "skills": ["{'name': '%s'}" % secret, "safe-skill"],
            "enabled_toolsets": ["{'web': '%s'}" % secret],
            "name": {"display": secret},
            "name_is_explicit": True,
        },
        source_profile="writer",
    )
    serialized = json.dumps(record)
    assert secret not in serialized
    assert "skill" not in record["job"]
    assert "skills" not in record["job"]
    assert "enabled_toolsets" not in record["job"]
    assert "name" not in record["job"]

    with pytest.raises(ValueError, match="safe identifier string"):
        event_bus.build_cron_event(
            "update",
            job={"id": {"private": secret}},
            source_profile="writer",
        )


def test_dry_run_builds_event_without_writing(isolated_profile_cron):
    _root, _profile_home, events_dir = isolated_profile_cron
    from cron import event_bus
    record = event_bus.publish_cron_event("update", job={"id": "job123", "name": "Dry run job", "prompt": "do not serialize", "schedule_display": "0 9 * * *"}, source_profile="ops", dry_run=True)
    assert record["source_profile"] == "ops"
    assert record["job_id"] == "job123"
    assert "schedule_display" not in record["job"]
    assert "prompt" not in record["job"]
    assert not (events_dir / "pending" / "ops").exists()


@pytest.mark.parametrize("value", ["false", "no", "1", 1, None])
def test_malformed_non_boolean_config_does_not_enable_publication(
    monkeypatch, value
):
    from cron import event_bus

    monkeypatch.delenv("HERMES_CRON_EVENTS_ENABLED", raising=False)
    monkeypatch.setattr(
        event_bus,
        "load_config_readonly",
        lambda: {"cron": {"events": {"enabled": value}}},
    )

    assert event_bus.events_enabled() is False


def test_boolean_config_explicitly_enables_publication(monkeypatch):
    from cron import event_bus

    monkeypatch.delenv("HERMES_CRON_EVENTS_ENABLED", raising=False)
    monkeypatch.setattr(
        event_bus,
        "load_config_readonly",
        lambda: {"cron": {"events": {"enabled": True}}},
    )

    assert event_bus.events_enabled() is True


def test_first_publication_fsyncs_each_new_directory_boundary(tmp_path, monkeypatch):
    from cron import event_bus

    events_dir = tmp_path / "events" / "cron"
    monkeypatch.setenv("HERMES_CRON_EVENTS_DIR", str(events_dir))
    fsynced = []
    monkeypatch.setattr(event_bus, "_SECURE_DIR_FD", False)
    monkeypatch.setattr(event_bus, "_fsync_directory", fsynced.append)

    event_bus.publish_cron_event(
        "create", job={"id": "durable-first", "name": "Durable"}, source_profile="ops"
    )

    assert tmp_path in fsynced
    assert tmp_path / "events" in fsynced
    assert events_dir in fsynced
    assert events_dir / "pending" in fsynced
    assert events_dir / "pending" / "ops" in fsynced


def test_profile_owner_is_normalized_consistently_in_record_and_directory(
    isolated_profile_cron,
):
    _root, _profile_home, events_dir = isolated_profile_cron
    from cron import event_bus

    record = event_bus.publish_cron_event(
        "create",
        job={"id": "job-profile", "name": "Profile-safe job"},
        source_profile="ops/team west",
    )

    assert record["source_profile"] == "ops_team_west"
    paths = list((events_dir / "pending" / "ops_team_west").glob("*.json"))
    assert len(paths) == 1
    assert json.loads(paths[0].read_text())["source_profile"] == "ops_team_west"


@pytest.mark.parametrize("profile", [".", ".."])
def test_dot_profile_segments_are_rejected(isolated_profile_cron, profile):
    from cron import event_bus

    with pytest.raises(ValueError, match="profile name"):
        event_bus.publish_cron_event(
            "create", job={"id": "unsafe-profile"}, source_profile=profile
        )


def test_publisher_rejects_symlinked_profile_directory(tmp_path, monkeypatch):
    from cron import event_bus

    events_dir = tmp_path / "events" / "cron"
    external = tmp_path / "external"
    external.mkdir()
    profile_dir = events_dir / "pending" / "writer"
    profile_dir.parent.mkdir(parents=True)
    profile_dir.symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("HERMES_CRON_EVENTS_DIR", str(events_dir))

    with pytest.raises(ValueError, match="symlink component"):
        event_bus.publish_cron_event(
            "create", job={"id": "symlinked"}, source_profile="writer"
        )

    assert not list(external.iterdir())


def test_publisher_dirfd_blocks_ancestor_symlink_swap(tmp_path, monkeypatch):
    from cron import event_bus

    if not event_bus._SECURE_DIR_FD:
        pytest.skip("requires POSIX directory-fd support")
    events_dir = tmp_path / "events" / "cron"
    profile_dir = events_dir / "pending" / "writer"
    displaced = tmp_path / "displaced-writer"
    external = tmp_path / "external"
    external.mkdir()
    monkeypatch.setenv("HERMES_CRON_EVENTS_DIR", str(events_dir))
    original_open = event_bus._open_or_create_directory_fd
    swapped = False

    @contextmanager
    def open_then_swap(path):
        nonlocal swapped
        with original_open(path) as directory_fd:
            if path == profile_dir and not swapped:
                profile_dir.rename(displaced)
                profile_dir.symlink_to(external, target_is_directory=True)
                swapped = True
            yield directory_fd

    monkeypatch.setattr(event_bus, "_open_or_create_directory_fd", open_then_swap)

    event_bus.publish_cron_event(
        "create", job={"id": "publisher-race"}, source_profile="writer"
    )

    assert not list(external.iterdir())
    assert list(displaced.glob("*.json"))
    assert not list(profile_dir.glob("*.json"))


def test_concurrent_publication_creates_only_complete_records(tmp_path, monkeypatch):
    events_dir = tmp_path / "events" / "cron"
    monkeypatch.setenv("HERMES_CRON_EVENTS_DIR", str(events_dir))
    from cron import event_bus
    def publish(index: int):
        return event_bus.publish_cron_event("create", job={"id": f"job-{index}", "name": f"Job {index}"}, source_profile="ops")
    with ThreadPoolExecutor(max_workers=12) as pool:
        records = list(pool.map(publish, range(100)))
    paths = list((events_dir / "pending" / "ops").glob("*.json"))
    assert len(paths) == 100
    assert {json.loads(path.read_text())["event_id"] for path in paths} == {record["event_id"] for record in records}
    assert not list(events_dir.rglob("*.tmp"))
