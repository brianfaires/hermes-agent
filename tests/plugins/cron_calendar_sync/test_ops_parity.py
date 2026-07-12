"""Executable contracts for the Ops calendar semantics owned by the plugin."""

from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from hermes_plugins.cron_calendar_sync import calendar_sync as cs
from hermes_plugins.cron_calendar_sync import calendar_client as client_module


class FakeCalendarClient:
    def __init__(self):
        self.events = {}
        self.instances = {}
        self.created = []
        self.patched = []
        self.deleted = []
        self.instance_queries = []
        self.fail_get = False
        self.fail_list = False
        self.fail_patch = False
        self.next_id = 1

    def create_event_body(self, calendar, body):
        event_id = f"ev{self.next_id}"
        self.next_id += 1
        event = deepcopy(body)
        event["id"] = event_id
        self.events[event_id] = event
        self.created.append((calendar, deepcopy(body)))
        return event_id

    def patch_event_body(self, calendar, event_id, body):
        if self.fail_patch:
            return False
        if event_id not in self.events:
            return False
        self.events[event_id].update(deepcopy(body))
        self.patched.append((calendar, event_id, deepcopy(body)))
        return True

    def get_event(self, calendar, event_id):
        if self.fail_get:
            raise client_module.CalendarOperationError("temporary read failure")
        event = self.events.get(event_id)
        return deepcopy(event) if event else None

    def list_events(self, calendar):
        if self.fail_list:
            raise client_module.CalendarOperationError("temporary list failure")
        return [deepcopy(event) for event in self.events.values()]

    def list_instances(self, calendar, event_id, time_min, time_max):
        self.instance_queries.append((event_id, time_min, time_max))
        return deepcopy(self.instances.get(event_id, []))

    def delete_event(self, calendar, event_id):
        self.deleted.append(event_id)
        self.events.pop(event_id, None)
        return True


@pytest.fixture()
def env(tmp_path, monkeypatch):
    fake = FakeCalendarClient()
    monkeypatch.setattr(cs, "CRON_DIR", tmp_path)
    monkeypatch.setattr(cs, "_enabled", lambda: True)
    monkeypatch.setattr(cs, "_calendar_id", lambda: "Hermes crons")
    monkeypatch.setattr(cs, "_iana_timezone", lambda: "America/Los_Angeles")
    monkeypatch.setattr(cs, "calendar_client", fake)
    return fake


def job(**overrides):
    value = {
        "id": "job1",
        "name": "My Job",
        "next_run_at": "2026-07-19T09:00:00-07:00",
        "schedule": {"kind": "cron", "expr": "0 */2 * * *"},
        "schedule_display": "0 */2 * * *",
    }
    value.update(overrides)
    return value


def test_high_frequency_schedule_is_one_all_day_daily_series():
    plan = cs._plan_events(job())

    assert plan == [
        {
            "start": {"date": "2026-07-19"},
            "end": {"date": "2026-07-20"},
            "recurrence": "RRULE:FREQ=DAILY;INTERVAL=1",
            "mode": "all-day-high-frequency",
        }
    ]


def test_update_patches_in_place_without_overwriting_manual_title(env):
    cs.on_create(job())
    event_id = cs._load_state()["job1"]["events"][0]["event_id"]
    env.events[event_id]["summary"] = "🛠 Brian's title"

    cs.on_update(job(name="Renamed", schedule={"kind": "cron", "expr": "0 */3 * * *"}))

    assert list(env.events) == [event_id]
    assert env.events[event_id]["summary"] == "🛠 Brian's title"
    assert env.deleted == []
    assert all("summary" not in body for _, patched_id, body in env.patched if patched_id == event_id)


def test_remove_archives_series_and_retains_event_history(env, monkeypatch):
    cs.on_create(job(schedule={"kind": "cron", "expr": "0 9 * * *"}))
    event_id = cs._load_state()["job1"]["events"][0]["event_id"]
    monkeypatch.setattr(cs, "_utc_until_before", lambda now=None: "20260720T000000Z")

    cs.on_remove(job())

    assert event_id in env.events
    assert env.deleted == []
    assert env.events[event_id]["recurrence"] == ["RRULE:FREQ=DAILY;UNTIL=20260720T000000Z"]
    assert "Archived by Hermes cron calendar sync" in env.events[event_id]["description"]
    state = cs._load_state()
    assert "job1" not in state
    assert state["archived_events"]["job1"]["event_id"] == event_id


def test_missing_state_adopts_live_managed_event(env):
    env.events["existing"] = {
        "id": "existing",
        "summary": "✍️ Brian kept this",
        "description": "old",
        "start": {"date": "2026-07-19"},
        "end": {"date": "2026-07-20"},
        "recurrence": ["RRULE:FREQ=DAILY;INTERVAL=1"],
        "extendedProperties": {
            "private": {"managedBy": cs.MANAGED_BY, "hermesCronJobId": "job1"}
        },
    }

    cs.on_update(job())

    assert cs._load_state()["job1"]["events"][0]["event_id"] == "existing"
    assert env.created == []
    assert env.events["existing"]["summary"] == "✍️ Brian kept this"


def test_complete_attaches_sanitized_response_to_instance_not_master(env, tmp_path):
    cs.on_create(job(schedule={"kind": "cron", "expr": "0 9 * * *"}))
    event_id = cs._load_state()["job1"]["events"][0]["event_id"]
    env.instances[event_id] = [
        {
            "id": "instance-1",
            "description": "instance notes",
            "start": {"dateTime": "2026-07-19T09:00:00-07:00"},
        }
    ]
    env.events["instance-1"] = deepcopy(env.instances[event_id][0])
    output = tmp_path / "2026-07-19_09-00-00.md"
    output.write_text(
        "# Cron output\n\n## Prompt\nprivate prompt\n\n"
        "## Response\ncompleted\napi_key=abcdefghijklmnopqrstuvwxyz\n"
        "sk-abcdefghijklmnopqrstuvwxyz0123456789\n"
    )

    cs.on_complete(
        job(schedule={"kind": "cron", "expr": "0 9 * * *"}),
        success=True,
        duration_seconds=10,
        output_file=str(output),
    )

    assert "private prompt" not in env.events["instance-1"]["description"]
    assert "completed" in env.events["instance-1"]["description"]
    assert "abcdefghijklmnopqrstuvwxyz" not in env.events["instance-1"]["description"]
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in env.events["instance-1"]["description"]
    assert "REDACTED" in env.events["instance-1"]["description"]
    assert "Hermes cron execution output" not in env.events[event_id]["description"]


def test_failed_create_does_not_discard_adoptable_live_event(env, monkeypatch):
    env.events["survivor"] = {
        "id": "survivor",
        "summary": "Surviving event",
        "extendedProperties": {
            "private": {"managedBy": cs.MANAGED_BY, "hermesCronJobId": "job1"}
        },
    }
    monkeypatch.setattr(env, "create_event_body", lambda *_: None)

    cs.on_update(job())

    assert cs._load_state()["job1"]["events"][0]["event_id"] == "survivor"
    assert "survivor" in env.events


def test_paused_update_archives_instead_of_leaving_active_series(env):
    cs.on_create(job(schedule={"kind": "cron", "expr": "0 9 * * *"}))
    event_id = cs._load_state()["job1"]["events"][0]["event_id"]

    cs.on_update(job(enabled=False, state="paused"))

    assert "job1" not in cs._load_state()
    assert "Archived by Hermes cron calendar sync" in env.events[event_id]["description"]


def test_complete_after_remove_uses_archived_event_for_final_output(env, tmp_path):
    active_job = job(schedule={"kind": "cron", "expr": "0 9 * * *"})
    cs.on_create(active_job)
    event_id = cs._load_state()["job1"]["events"][0]["event_id"]
    env.instances[event_id] = [
        {
            "id": "final-instance",
            "description": "instance",
            "start": {"dateTime": "2026-07-19T09:00:00-07:00"},
        }
    ]
    env.events["final-instance"] = deepcopy(env.instances[event_id][0])
    output = tmp_path / "2026-07-19_09-01-00.md"
    output.write_text("## Response\nfinal bounded result")

    cs.on_remove(active_job)
    cs.on_complete(active_job, success=True, duration_seconds=60, output_file=str(output))

    assert "final bounded result" in env.events["final-instance"]["description"]


def test_transient_read_failure_does_not_create_duplicate_or_drop_state(env):
    cs.on_create(job())
    event_id = cs._load_state()["job1"]["events"][0]["event_id"]
    env.fail_get = True
    env.fail_list = True

    cs.on_update(job(name="changed"))
    cs.on_remove(job())

    assert list(env.events) == [event_id]
    assert len(env.created) == 1
    assert cs._load_state()["job1"]["events"][0]["event_id"] == event_id


def test_transition_to_nonrecurring_explicitly_clears_recurrence(env):
    cs.on_create(job(schedule={"kind": "cron", "expr": "0 9 * * *"}))
    event_id = cs._load_state()["job1"]["events"][0]["event_id"]

    cs.on_update(job(schedule={"kind": "once"}))

    assert env.events[event_id]["recurrence"] == []


def test_low_frequency_multislot_cron_remains_one_recurring_series():
    plan = cs._plan_events(job(schedule={"kind": "cron", "expr": "0 0,12 * * *"}))

    assert plan[0]["mode"] == "recurring"
    assert plan[0]["recurrence"] == "RRULE:FREQ=DAILY;BYHOUR=0,12;BYMINUTE=0"


def test_failed_resize_does_not_advance_learned_max(env):
    cs.on_create(job(schedule={"kind": "cron", "expr": "0 9 * * *"}))
    env.fail_patch = True

    cs.on_complete(job(), success=True, duration_seconds=600)

    assert cs._load_state()["job1"]["max_duration_seconds"] is None


def test_long_run_uses_start_time_when_querying_instances(env, tmp_path):
    active_job = job(schedule={"kind": "cron", "expr": "0 9 * * *"})
    cs.on_create(active_job)
    output = tmp_path / "2026-07-19_17-00-00.md"
    output.write_text("## Response\ndone")

    cs.on_complete(active_job, success=True, duration_seconds=8 * 3600, output_file=str(output))

    _, time_min, _ = env.instance_queries[-1]
    assert datetime.fromisoformat(time_min).hour <= 9


def test_high_frequency_output_is_recorded_as_skipped_not_uploaded(env, tmp_path):
    cs.on_create(job())
    event_id = cs._load_state()["job1"]["events"][0]["event_id"]
    env.instances[event_id] = [
        {"id": "all-day-instance", "start": {"date": "2026-07-19"}, "description": ""}
    ]
    env.events["all-day-instance"] = deepcopy(env.instances[event_id][0])
    output = tmp_path / "2026-07-19_09-00-00.md"
    output.write_text("## Response\nfrequent result")

    cs.on_complete(job(), success=True, duration_seconds=1, output_file=str(output))

    assert "frequent result" not in env.events["all-day-instance"]["description"]
    tracked = cs._load_state()["run_outputs"]["job1"][output.name]
    assert tracked["skipped"] is True


def test_redaction_covers_basic_auth_and_secret_environment_names():
    text = (
        "Authorization: Basic dXNlcjpwYXNzd29yZA==\n"
        "AWS_SECRET_ACCESS_KEY=abcdefghijklmnopqrstuvwx"
    )

    redacted = cs._redact_secrets(text)

    assert "dXNlcjpwYXNzd29yZA" not in redacted
    assert "abcdefghijklmnopqrstuvwx" not in redacted


def test_one_shot_complete_archives_after_processing_final_output(env, tmp_path):
    once = job(schedule={"kind": "once"})
    cs.on_create(once)
    event_id = cs._load_state()["job1"]["events"][0]["event_id"]
    output = tmp_path / "2026-07-19_09-00-10.md"
    output.write_text("## Response\none-shot done")

    cs.on_complete(once, success=True, duration_seconds=10, output_file=str(output))

    state = cs._load_state()
    assert "job1" not in state
    assert state["archived_events"]["job1"]["event_id"] == event_id
    assert "Archived by Hermes cron calendar sync" in env.events[event_id]["description"]
    assert "one-shot done" in env.events[event_id]["description"]
