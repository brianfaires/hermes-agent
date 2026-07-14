"""Tests for cron_calendar_sync hook handlers, RRULE mapping, and slot expansion."""

import pytest

from hermes_plugins.cron_calendar_sync import calendar_sync as cs


# --------------------------------------------------------------------------- #
# RRULE mapping (daily and coarser)
# --------------------------------------------------------------------------- #

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
        assert cs._cron_rrule("0 9 1 1 *") == "RRULE:FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1"

    def test_cron_subdaily_has_no_single_rrule(self):
        assert cs._cron_rrule("*/15 * * * *") is None
        assert cs._cron_rrule("0 */2 * * *") is None


# --------------------------------------------------------------------------- #
# Intraday slot expansion / event planning
# --------------------------------------------------------------------------- #

class TestPlanEvents:
    def _job(self, schedule, anchor="2026-06-23T09:00:00-07:00"):
        return {"id": "j", "name": "n", "next_run_at": anchor, "schedule": schedule}

    def test_once_single_event(self):
        plan = cs._plan_events(self._job({"kind": "once"}))
        assert plan == [{"start": "2026-06-23T09:00:00-07:00", "recurrence": None}]

    def test_daily_single_recurring(self):
        plan = cs._plan_events(self._job({"kind": "cron", "expr": "0 7 * * *"},
                                          anchor="2026-06-23T07:00:00-07:00"))
        assert len(plan) == 1
        assert plan[0]["recurrence"] == "RRULE:FREQ=DAILY"

    def test_interval_subdaily_expands(self):
        # every 12h -> 2 daily slots
        plan = cs._plan_events(self._job({"kind": "interval", "minutes": 720}))
        assert len(plan) == 2
        assert all(p["recurrence"] == "RRULE:FREQ=DAILY" for p in plan)
        starts = sorted(p["start"] for p in plan)
        assert starts == ["2026-06-23T09:00:00-07:00", "2026-06-23T21:00:00-07:00"]

    def test_cron_every_two_hours_expands_to_12(self):
        plan = cs._plan_events(self._job({"kind": "cron", "expr": "0 */2 * * *"},
                                          anchor="2026-06-23T00:00:00-07:00"))
        assert len(plan) == 12
        assert all(p["recurrence"] == "RRULE:FREQ=DAILY" for p in plan)

    def test_cron_every_15_min_expands_to_96(self):
        plan = cs._plan_events(self._job({"kind": "cron", "expr": "*/15 * * * *"},
                                          anchor="2026-06-23T00:00:00-07:00"))
        assert len(plan) == 96

    def test_over_cap_falls_back_to_single(self):
        # every minute -> 1440 slots > cap -> single event
        plan = cs._plan_events(self._job({"kind": "cron", "expr": "* * * * *"},
                                          anchor="2026-06-23T00:00:00-07:00"))
        assert plan == [{"start": "2026-06-23T00:00:00-07:00", "recurrence": None}]

    def test_no_next_run_no_plan(self):
        assert cs._plan_events({"id": "j", "schedule": {"kind": "once"}, "next_run_at": None}) == []


# --------------------------------------------------------------------------- #
# Handlers (calendar client mocked)
# --------------------------------------------------------------------------- #

class FakeClient:
    def __init__(self):
        self.created = []
        self.updated = []
        self.deleted = []
        self.next_id = 1

    def create_event(self, calendar, summary, start, end, *, recurrence=None,
                     timezone=None, description=None):
        eid = f"ev{self.next_id}"
        self.next_id += 1
        self.created.append(dict(calendar=calendar, summary=summary, start=start,
                                 end=end, recurrence=recurrence, timezone=timezone,
                                 event_id=eid))
        return eid

    def update_event(self, calendar, event_id, **kwargs):
        self.updated.append(dict(calendar=calendar, event_id=event_id, **kwargs))
        return True

    def delete_event(self, calendar, event_id):
        self.deleted.append(event_id)
        return True


@pytest.fixture()
def env(tmp_path, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(cs, "CRON_DIR", tmp_path)
    monkeypatch.setattr(cs, "_enabled", lambda: True)
    monkeypatch.setattr(cs, "_calendar_id", lambda: "Hermes crons")
    monkeypatch.setattr(cs, "_iana_timezone", lambda: "America/Los_Angeles")
    monkeypatch.setattr(cs, "calendar_client", fake)
    return fake


def _job(**over):
    job = {
        "id": "job1",
        "name": "My Job",
        "next_run_at": "2026-06-23T09:00:00-07:00",
        "schedule": {"kind": "interval", "minutes": 1440},
        "schedule_display": "every 1d",
    }
    job.update(over)
    return job


def test_on_create_makes_event_and_state(env):
    cs.on_create(_job())

    assert len(env.created) == 1
    ev = env.created[0]
    assert ev["recurrence"] == "RRULE:FREQ=DAILY;INTERVAL=1"
    assert ev["start"] == "2026-06-23T09:00:00-07:00"
    assert ev["end"] == "2026-06-23T09:05:00-07:00"  # 300s default baseline

    state = cs._load_state()
    assert state["job1"]["events"][0]["event_id"] == "ev1"
    assert state["job1"]["max_duration_seconds"] is None


def test_on_create_subdaily_makes_multiple_daily_events(env):
    cs.on_create(_job(schedule={"kind": "cron", "expr": "0 */2 * * *"},
                      next_run_at="2026-06-23T00:00:00-07:00"))
    assert len(env.created) == 12
    assert all(c["recurrence"] == "RRULE:FREQ=DAILY" for c in env.created)
    assert len(cs._load_state()["job1"]["events"]) == 12


def test_on_create_no_next_run_skips(env):
    cs.on_create(_job(next_run_at=None))
    assert env.created == []
    assert cs._load_state() == {}


def test_on_update_recreates_events(env):
    cs.on_create(_job())
    cs.on_update(_job(name="Renamed"))

    assert env.deleted == ["ev1"]          # old event removed
    assert env.created[-1]["summary"] == "⏰ Renamed"
    assert cs._load_state()["job1"]["events"][0]["event_id"] == "ev2"


def test_on_remove_deletes_all_and_clears_state(env):
    cs.on_create(_job(schedule={"kind": "cron", "expr": "0 */2 * * *"},
                      next_run_at="2026-06-23T00:00:00-07:00"))
    cs.on_remove(_job())

    assert len(env.deleted) == 12
    assert "job1" not in cs._load_state()


def test_on_complete_first_run_sets_baseline_silently(env):
    cs.on_create(_job())
    notes = []
    cs.on_complete(_job(), success=True, duration_seconds=420.0,
                   notify=lambda m, warn=False: notes.append((m, warn)))

    assert notes == []  # silent on first run
    assert cs._load_state()["job1"]["max_duration_seconds"] == 420.0
    assert env.updated[-1]["end"] == "2026-06-23T09:07:00-07:00"  # 09:00 + 420s


def test_on_complete_tiny_duration_floored_in_event_only(env):
    cs.on_create(_job())
    cs.on_complete(_job(), success=True, duration_seconds=42.0, notify=lambda *a, **k: None)
    assert cs._load_state()["job1"]["max_duration_seconds"] == 42.0
    assert env.updated[-1]["end"] == "2026-06-23T09:01:00-07:00"  # floored to MIN 60s


def test_on_complete_resizes_all_subdaily_events(env):
    cs.on_create(_job(schedule={"kind": "cron", "expr": "0 */2 * * *"},
                      next_run_at="2026-06-23T00:00:00-07:00"))
    cs.on_complete(_job(schedule={"kind": "cron", "expr": "0 */2 * * *"}),
                   success=True, duration_seconds=600.0, notify=lambda *a, **k: None)
    # all 12 events resized
    assert len(env.updated) == 12


def test_on_complete_increase_notifies(env):
    cs.on_create(_job())
    cs.on_complete(_job(), success=True, duration_seconds=42.0, notify=lambda *a, **k: None)

    notes = []
    cs.on_complete(_job(), success=True, duration_seconds=70.0,
                   notify=lambda m, warn=False: notes.append((m, warn)))

    assert len(notes) == 1
    msg, warn = notes[0]
    assert "increased cron max_duration from 42s -> 70s" in msg
    assert warn is False
    assert cs._load_state()["job1"]["max_duration_seconds"] == 70.0


def test_on_complete_small_increase_updates_without_notify(env):
    cs.on_create(_job())
    cs.on_complete(_job(), success=True, duration_seconds=50.0, notify=lambda *a, **k: None)

    notes = []
    cs.on_complete(_job(), success=True, duration_seconds=75.0,
                   notify=lambda m, warn=False: notes.append((m, warn)))

    assert notes == []
    assert cs._load_state()["job1"]["max_duration_seconds"] == 75.0
    assert env.updated[-1]["end"] == "2026-06-23T09:01:15-07:00"


def test_on_complete_alerts_only_above_fifty_percent_growth(env):
    cs.on_create(_job())
    cs.on_complete(_job(), success=True, duration_seconds=50.0, notify=lambda *a, **k: None)

    notes = []
    cs.on_complete(_job(), success=True, duration_seconds=76.0,
                   notify=lambda m, warn=False: notes.append((m, warn)))

    assert len(notes) == 1
    msg, warn = notes[0]
    assert "increased cron max_duration from 50s -> 76s" in msg
    assert warn is False


def test_on_complete_doubling_escalates_to_warning(env):
    cs.on_create(_job())
    cs.on_complete(_job(), success=True, duration_seconds=30.0, notify=lambda *a, **k: None)

    notes = []
    cs.on_complete(_job(), success=True, duration_seconds=80.0,
                   notify=lambda m, warn=False: notes.append((m, warn)))

    assert len(notes) == 1
    msg, warn = notes[0]
    assert warn is True
    assert "cron took longer than expected" in msg


def test_on_complete_no_growth_is_noop(env):
    cs.on_create(_job())
    cs.on_complete(_job(), success=True, duration_seconds=50.0, notify=lambda *a, **k: None)
    env.updated.clear()

    notes = []
    cs.on_complete(_job(), success=True, duration_seconds=20.0,
                   notify=lambda m, warn=False: notes.append((m, warn)))

    assert notes == []
    assert env.updated == []
    assert cs._load_state()["job1"]["max_duration_seconds"] == 50.0


def test_on_complete_failure_ignored(env):
    cs.on_create(_job())
    cs.on_complete(_job(), success=False, duration_seconds=999.0, notify=lambda *a, **k: None)
    assert cs._load_state()["job1"]["max_duration_seconds"] is None


def test_on_complete_untracked_job_noop(env):
    cs.on_complete(_job(id="ghost"), success=True, duration_seconds=10.0,
                   notify=lambda *a, **k: None)
    assert cs._load_state() == {}


def test_disabled_skips_everything(env, monkeypatch):
    monkeypatch.setattr(cs, "_enabled", lambda: False)
    cs.on_create(_job())
    assert env.created == []
