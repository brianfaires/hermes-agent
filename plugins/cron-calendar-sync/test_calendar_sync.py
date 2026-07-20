"""Tests for cron-calendar-sync schedules, lifecycle hooks, and duration learning."""

from copy import deepcopy

import pytest

from hermes_plugins.cron_calendar_sync import calendar_sync as cs


class TestRruleMapping:
    def test_interval_daily(self):
        assert cs._rrule_for_schedule({"kind": "interval", "minutes": 1440}) == "RRULE:FREQ=DAILY;INTERVAL=1"

    def test_interval_multi_day(self):
        assert cs._rrule_for_schedule({"kind": "interval", "minutes": 2880}) == "RRULE:FREQ=DAILY;INTERVAL=2"

    def test_interval_subdaily_has_no_single_rrule(self):
        assert cs._rrule_for_schedule({"kind": "interval", "minutes": 30}) is None

    def test_cron_daily(self):
        assert cs._cron_rrule("0 9 * * *") == "RRULE:FREQ=DAILY"

    def test_cron_weekly(self):
        assert cs._cron_rrule("0 9 * * 1,3,5") == "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"

    def test_cron_monthly(self):
        assert cs._cron_rrule("0 9 1 * *") == "RRULE:FREQ=MONTHLY;BYMONTHDAY=1"

    def test_cron_yearly(self):
        assert cs._cron_rrule("0 9 1 1 *") == "RRULE:FREQ=MONTHLY;BYMONTHDAY=1;BYMONTH=1"

    def test_cron_multislot_rrules_are_parseable_even_when_planned_all_day(self):
        assert cs._cron_rrule("*/15 * * * *") == "RRULE:FREQ=DAILY;BYMINUTE=0,15,30,45"
        assert cs._cron_rrule("0 */2 * * *") == "RRULE:FREQ=DAILY;BYHOUR=0,2,4,6,8,10,12,14,16,18,20,22;BYMINUTE=0"


class TestPlanEvents:
    def _job(self, schedule, anchor="2026-06-23T09:00:00-07:00"):
        return {"id": "j", "name": "n", "next_run_at": anchor, "schedule": schedule}

    def test_once_is_timed_single_event(self):
        plan = cs._plan_events(self._job({"kind": "once"}))
        assert len(plan) == 1
        assert plan[0]["mode"] == "one-shot"
        assert plan[0]["recurrence"] is None
        assert plan[0]["start"]["dateTime"] == "2026-06-23T09:00:00-07:00"

    def test_daily_is_one_timed_recurring_series(self):
        plan = cs._plan_events(self._job({"kind": "cron", "expr": "0 7 * * *"}))
        assert len(plan) == 1
        assert plan[0]["recurrence"] == "RRULE:FREQ=DAILY"
        assert plan[0]["mode"] == "recurring"

    @pytest.mark.parametrize(
        "schedule",
        [
            {"kind": "interval", "minutes": 360},
            {"kind": "cron", "expr": "0 */2 * * *"},
            {"kind": "cron", "expr": "*/15 * * * *"},
            {"kind": "cron", "expr": "* * * * *"},
        ],
    )
    def test_high_frequency_is_one_all_day_series(self, schedule):
        plan = cs._plan_events(self._job(schedule))
        assert len(plan) == 1
        assert plan[0]["mode"] == "all-day-high-frequency"
        assert plan[0]["recurrence"] == "RRULE:FREQ=DAILY;INTERVAL=1"
        assert plan[0]["start"] == {"date": "2026-06-23"}

    def test_over_six_hours_is_not_all_day(self):
        plan = cs._plan_events(self._job({"kind": "interval", "minutes": 361}))
        assert plan[0]["mode"] != "all-day-high-frequency"

    def test_no_next_run_no_plan(self):
        assert cs._plan_events({"id": "j", "schedule": {"kind": "once"}, "next_run_at": None}) == []


class FakeClient:
    def __init__(self):
        self.events = {}
        self.instances = {}
        self.created = []
        self.patched = []
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
        event = self.events.get(event_id)
        if event is None:
            return False
        event.update(deepcopy(body))
        self.patched.append((calendar, event_id, deepcopy(body)))
        return True

    def get_event(self, calendar, event_id):
        event = self.events.get(event_id)
        return deepcopy(event) if event else None

    def list_events(self, calendar):
        return [deepcopy(event) for event in self.events.values()]

    def list_instances(self, calendar, event_id, time_min, time_max):
        return deepcopy(self.instances.get(event_id, []))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(cs, "CRON_DIR", tmp_path)
    monkeypatch.setattr(cs, "_enabled", lambda: True)
    monkeypatch.setattr(cs, "_calendar_id", lambda: "Hermes crons")
    monkeypatch.setattr(cs, "_iana_timezone", lambda: "America/Los_Angeles")
    monkeypatch.setattr(cs, "calendar_client", fake)
    return fake


def _job(**overrides):
    value = {
        "id": "job1",
        "name": "My Job",
        "next_run_at": "2026-06-23T09:00:00-07:00",
        "schedule": {"kind": "interval", "minutes": 1440},
        "schedule_display": "every 1d",
    }
    value.update(overrides)
    return value


def test_on_create_makes_event_and_state(env):
    cs.on_create(_job())
    assert len(env.created) == 1
    event = env.created[0][1]
    assert event["recurrence"] == ["RRULE:FREQ=DAILY;INTERVAL=1"]
    assert event["start"]["dateTime"] == "2026-06-23T09:00:00-07:00"
    state = cs._load_state()
    assert state["job1"]["events"][0]["event_id"] == "ev1"
    assert state["job1"]["max_duration_seconds"] is None


def test_on_create_no_next_run_skips(env):
    cs.on_create(_job(next_run_at=None))
    assert env.created == []
    assert cs._load_state() == {}


def test_on_complete_first_run_sets_baseline_silently(env):
    cs.on_create(_job())
    notes = []
    cs.on_complete(_job(), success=True, duration_seconds=420.0, notify=lambda m, warn=False: notes.append((m, warn)))
    assert notes == []
    assert cs._load_state()["job1"]["max_duration_seconds"] == 420.0
    event = env.events["ev1"]
    assert event["end"]["dateTime"] == "2026-06-23T09:07:00-07:00"


def test_on_complete_tiny_duration_is_floored_only_in_event(env):
    cs.on_create(_job())
    cs.on_complete(_job(), success=True, duration_seconds=42.0)
    assert cs._load_state()["job1"]["max_duration_seconds"] == 42.0
    assert env.events["ev1"]["end"]["dateTime"] == "2026-06-23T09:01:00-07:00"


def test_on_complete_increase_notifies_above_threshold(env):
    cs.on_create(_job())
    cs.on_complete(_job(), success=True, duration_seconds=50.0)
    notes = []
    cs.on_complete(_job(), success=True, duration_seconds=76.0, notify=lambda m, warn=False: notes.append((m, warn)))
    assert len(notes) == 1
    assert notes[0][1] is False
    assert "50s -> 76s" in notes[0][0]


def test_on_complete_doubling_warns(env):
    cs.on_create(_job())
    cs.on_complete(_job(), success=True, duration_seconds=30.0)
    notes = []
    cs.on_complete(_job(), success=True, duration_seconds=80.0, notify=lambda m, warn=False: notes.append((m, warn)))
    assert notes[0][1] is True
    assert "cron took longer than expected" in notes[0][0]


def test_on_complete_no_growth_is_noop(env):
    cs.on_create(_job())
    cs.on_complete(_job(), success=True, duration_seconds=50.0)
    env.patched.clear()
    cs.on_complete(_job(), success=True, duration_seconds=20.0)
    assert env.patched == []
    assert cs._load_state()["job1"]["max_duration_seconds"] == 50.0


def test_on_complete_failure_does_not_update_duration(env):
    cs.on_create(_job())
    cs.on_complete(_job(), success=False, duration_seconds=999.0)
    assert cs._load_state()["job1"]["max_duration_seconds"] is None


def test_on_complete_untracked_job_noop(env):
    cs.on_complete(_job(id="ghost"), success=True, duration_seconds=10.0)
    assert cs._load_state() == {}


def test_disabled_skips_everything(env, monkeypatch):
    monkeypatch.setattr(cs, "_enabled", lambda: False)
    cs.on_create(_job())
    assert env.created == []
