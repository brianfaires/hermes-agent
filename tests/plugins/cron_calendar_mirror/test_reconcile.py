"""Reconciliation contract: lifecycle, idempotence, failure, and blast radius."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from plugins.cron_calendar_mirror.mirror import (
    KIND_DIGEST,
    KIND_RUN,
    KIND_SCHEDULE,
    PROP_EXECUTION,
    PROP_JOB,
    PROP_KIND,
    PROP_PROFILE,
    PROP_STATUS,
    ConfigurationError,
    Settings,
    build_schedule_events,
    event_id,
    reconcile,
)
from plugins.cron_calendar_mirror.calendar_client import (
    CalendarError,
    CalendarNotFound,
)

from .conftest import UTC, FakeCalendar, make_execution, make_job

PROFILE = "ops"


def run(fake, *, settings, jobs=(), executions=(), now, dry_run=False, profile=PROFILE):
    return reconcile(
        fake,
        settings=settings,
        jobs=list(jobs),
        executions=list(executions),
        profile=profile,
        now=now,
        tz_name="UTC",
        dry_run=dry_run,
    )


@pytest.fixture
def daily_job(now):
    return make_job(
        "job-daily",
        name="Nightly digest",
        schedule={"kind": "cron", "expr": "0 9 * * *", "display": "every day at 9am"},
        next_run_at=datetime(2026, 9, 7, 9, 0, tzinfo=UTC),
    )


@pytest.fixture
def two_day(settings):
    return replace(settings, horizon_days=2)


# ---------------------------------------------------------------------------
# Create / update / idempotence
# ---------------------------------------------------------------------------

def test_creating_a_job_creates_forecast_events(fake_calendar, two_day, daily_job, now):
    report = run(fake_calendar, settings=two_day, jobs=[daily_job], now=now)

    assert report.ok
    assert report.created == 2
    assert report.desired == 2
    assert fake_calendar.ids_of_kind(KIND_SCHEDULE) == sorted(fake_calendar.events)
    for event in fake_calendar.events.values():
        private = event["extendedProperties"]["private"]
        assert private[PROP_PROFILE] == PROFILE
        assert private[PROP_JOB] == "job-daily"


def test_second_reconcile_writes_nothing(fake_calendar, two_day, daily_job, now):
    run(fake_calendar, settings=two_day, jobs=[daily_job], now=now)
    fake_calendar.calls.clear()

    report = run(fake_calendar, settings=two_day, jobs=[daily_job], now=now)

    assert fake_calendar.write_calls() == []
    assert report.unchanged == 2
    assert report.created == report.updated == report.archived == 0


def test_rescheduling_a_job_replaces_its_forecast(fake_calendar, two_day, daily_job, now):
    run(fake_calendar, settings=two_day, jobs=[daily_job], now=now)
    before = set(fake_calendar.events)

    moved = dict(
        daily_job,
        schedule={"kind": "cron", "expr": "0 18 * * *", "display": "every day at 6pm"},
        schedule_display="every day at 6pm",
        next_run_at=datetime(2026, 9, 6, 18, 0, tzinfo=UTC).isoformat(),
    )
    report = run(fake_calendar, settings=two_day, jobs=[moved], now=now)

    assert report.created == 2
    assert report.archived == 2
    assert before.issubset(fake_calendar.events)
    assert all(fake_calendar.events[k]["extendedProperties"]["private"][PROP_STATUS] == "archived" for k in before)


def test_updates_preserve_a_hand_edited_title(fake_calendar, two_day, daily_job, now):
    run(fake_calendar, settings=two_day, jobs=[daily_job], now=now)
    target = sorted(fake_calendar.events)[0]
    fake_calendar.events[target]["summary"] = "My own title"
    # Force an update by changing something the description renders.
    renamed = dict(daily_job, name="Nightly digest v2")

    report = run(fake_calendar, settings=two_day, jobs=[renamed], now=now)

    assert report.updated == 2
    assert fake_calendar.events[target]["summary"] == "My own title"
    assert "Nightly digest v2" in fake_calendar.events[target]["description"]


# ---------------------------------------------------------------------------
# Pause / resume / remove
# ---------------------------------------------------------------------------

def test_pausing_archives_the_forecast_and_keeps_run_history(
    fake_calendar, two_day, daily_job, now
):
    execution = make_execution(
        "exec-1", "job-daily",
        claimed_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2) + timedelta(minutes=4),
    )
    run(fake_calendar, settings=two_day, jobs=[daily_job], executions=[execution], now=now)
    assert len(fake_calendar.ids_of_kind(KIND_SCHEDULE)) == 2

    paused = dict(daily_job, enabled=False, state="paused", paused_at=now.isoformat())
    report = run(fake_calendar, settings=two_day, jobs=[paused],
                 executions=[execution], now=now)

    assert report.archived == 2
    assert report.kept_history == 0  # the run event is still desired, so not "kept"
    assert all(fake_calendar.events[k]["extendedProperties"]["private"][PROP_STATUS] == "archived" for k in fake_calendar.ids_of_kind(KIND_SCHEDULE))
    assert len(fake_calendar.ids_of_kind(KIND_RUN)) == 1


def test_resume_restores_live_archived_ids_without_deletion(
    two_day, daily_job, now
):
    """Archived forecasts keep live IDs; resume needs no tombstone revival."""
    fake = FakeCalendar()
    run(fake, settings=two_day, jobs=[daily_job], now=now)
    original = set(fake.events)

    paused = dict(daily_job, enabled=False, state="paused", paused_at=now.isoformat())
    run(fake, settings=two_day, jobs=[paused], now=now)
    assert set(fake.events) == original
    assert fake.reserved == set()

    report = run(fake, settings=two_day, jobs=[daily_job], now=now)

    assert report.ok
    assert set(fake.events) == original
    assert report.updated == 2 and report.created == 0
    assert all(event["status"] == "confirmed" for event in fake.events.values())


def test_removing_a_job_archives_the_forecast_but_keeps_results(
    fake_calendar, two_day, daily_job, now
):
    execution = make_execution(
        "exec-9", "job-daily",
        claimed_at=now - timedelta(hours=1),
        finished_at=now - timedelta(hours=1) + timedelta(seconds=30),
    )
    run(fake_calendar, settings=two_day, jobs=[daily_job], executions=[execution], now=now)

    report = run(fake_calendar, settings=two_day, jobs=[], executions=[execution], now=now)

    assert report.archived == 2
    assert all(fake_calendar.events[k]["extendedProperties"]["private"][PROP_STATUS] == "archived" for k in fake_calendar.ids_of_kind(KIND_SCHEDULE))
    run_ids = fake_calendar.ids_of_kind(KIND_RUN)
    assert len(run_ids) == 1
    # The title was written while the job still existed and is never rewritten;
    # the removal is recorded in the description instead.
    assert fake_calendar.events[run_ids[0]]["summary"] == "▶ Nightly digest"
    assert "no longer exists" in fake_calendar.events[run_ids[0]]["description"]


def test_a_result_first_seen_after_removal_is_titled_as_removed(
    fake_calendar, two_day, now
):
    execution = make_execution(
        "exec-orphan", "job-vanished",
        claimed_at=now - timedelta(hours=1),
        finished_at=now - timedelta(hours=1) + timedelta(seconds=5),
    )

    run(fake_calendar, settings=two_day, jobs=[], executions=[execution], now=now)

    run_ids = fake_calendar.ids_of_kind(KIND_RUN)
    assert len(run_ids) == 1
    assert "(removed)" in fake_calendar.events[run_ids[0]]["summary"]


def test_run_events_are_never_pruned_even_when_no_job_or_ledger_row_remains(
    fake_calendar, two_day, daily_job, now
):
    execution = make_execution(
        "exec-gone", "job-daily",
        claimed_at=now - timedelta(hours=3),
        finished_at=now - timedelta(hours=3) + timedelta(minutes=1),
    )
    run(fake_calendar, settings=two_day, jobs=[daily_job], executions=[execution], now=now)

    # Ledger pruned the record and the job is gone: history must survive.
    report = run(fake_calendar, settings=two_day, jobs=[], executions=[], now=now)

    assert report.kept_history == 1
    assert len(fake_calendar.ids_of_kind(KIND_RUN)) == 1


# ---------------------------------------------------------------------------
# Run results bind to the execution, not to a nearby occurrence
# ---------------------------------------------------------------------------

def test_result_binds_to_its_execution_not_an_adjacent_occurrence(
    fake_calendar, settings, now
):
    """Two occurrences a minute apart, one failing run: only that run is red."""
    job = make_job(
        "job-fast",
        name="Frequent job",
        schedule={"kind": "interval", "minutes": 61, "display": "every 61m"},
        next_run_at=now + timedelta(minutes=1),
    )
    failing = make_execution(
        "exec-fail", "job-fast",
        claimed_at=now - timedelta(minutes=59),
        finished_at=now - timedelta(minutes=58),
        status="failed",
        error="boom",
    )
    ok = make_execution(
        "exec-ok", "job-fast",
        claimed_at=now - timedelta(minutes=120),
        finished_at=now - timedelta(minutes=119),
    )

    run(fake_calendar, settings=settings, jobs=[job],
        executions=[failing, ok], now=now)

    failed_id = event_id(PROFILE, KIND_RUN, "exec-fail")
    ok_id = event_id(PROFILE, KIND_RUN, "exec-ok")
    assert fake_calendar.events[failed_id]["colorId"] == "11"
    assert fake_calendar.events[ok_id]["colorId"] == "10"
    assert fake_calendar.events[failed_id]["extendedProperties"]["private"][
        PROP_EXECUTION] == "exec-fail"
    for schedule_id in fake_calendar.ids_of_kind(KIND_SCHEDULE):
        description = fake_calendar.events[schedule_id]["description"]
        assert "failed" not in description
        assert "exec-" not in description


def test_two_executions_at_the_same_instant_get_distinct_events(
    fake_calendar, settings, now
):
    instant = now - timedelta(minutes=5)
    first = make_execution("exec-a", "job-x", claimed_at=instant,
                           finished_at=instant + timedelta(seconds=10))
    second = make_execution("exec-b", "job-x", claimed_at=instant,
                            finished_at=instant + timedelta(seconds=10))

    report = run(fake_calendar, settings=settings, executions=[first, second], now=now)

    assert report.created == 2
    assert len(fake_calendar.ids_of_kind(KIND_RUN)) == 2


def test_in_flight_execution_is_shown_then_updated_in_place(
    fake_calendar, settings, now
):
    started = now - timedelta(seconds=5)
    claimed = make_execution("exec-live", "job-x", claimed_at=started,
                             status="running", finished_at=None)
    run(fake_calendar, settings=settings, executions=[claimed], now=now)

    identifier = event_id(PROFILE, KIND_RUN, "exec-live")
    assert fake_calendar.events[identifier]["colorId"] == "5"
    assert "still in flight" in fake_calendar.events[identifier]["description"]
    fake_calendar.events[identifier]["summary"] = "renamed by hand"

    finished = make_execution("exec-live", "job-x", claimed_at=started,
                              finished_at=started + timedelta(minutes=3))
    report = run(fake_calendar, settings=settings, executions=[finished], now=now)

    assert report.updated == 1
    assert len(fake_calendar.ids_of_kind(KIND_RUN)) == 1
    assert fake_calendar.events[identifier]["colorId"] == "10"
    assert fake_calendar.events[identifier]["summary"] == "renamed by hand"
    assert fake_calendar.events[identifier]["extendedProperties"]["private"][
        PROP_STATUS] == "completed"


def test_failure_detail_is_redacted_and_can_be_switched_off(fake_calendar, settings, now):
    execution = make_execution(
        "exec-secret", "job-x",
        claimed_at=now - timedelta(minutes=10),
        finished_at=now - timedelta(minutes=9),
        status="failed",
        error="auth failed: Authorization: Bearer sk-ant-api03-AAAAAAAAAAAAAAAAAAAA",
    )

    run(fake_calendar, settings=settings, executions=[execution], now=now)
    identifier = event_id(PROFILE, KIND_RUN, "exec-secret")
    description = fake_calendar.events[identifier]["description"]
    assert "sk-ant-api03-AAAAAAAAAAAAAAAAAAAA" not in description
    assert "Status: failed" in description

    quiet = FakeCalendar()
    run(quiet, settings=replace(settings, include_error_detail=False),
        executions=[execution], now=now)
    assert "Error:" not in quiet.events[identifier]["description"]


def test_old_results_are_backfilled_without_an_age_cutoff(
    fake_calendar, settings, now
):
    stale = make_execution("exec-old", "job-x",
                           claimed_at=now - timedelta(days=30),
                           finished_at=now - timedelta(days=30))

    report = run(fake_calendar, settings=settings, executions=[stale], now=now)

    assert report.desired == 1
    assert len(fake_calendar.events) == 1


# ---------------------------------------------------------------------------
# Retry and partial-failure behaviour
# ---------------------------------------------------------------------------

def test_a_failed_write_is_reported_and_retried_without_duplicating(
    fake_calendar, two_day, daily_job, now
):
    identifiers = sorted(
        event.id
        for event in build_schedule_events(
            [daily_job], profile=PROFILE, settings=two_day, now=now, tz_name="UTC"
        )
    )
    fake_calendar.fail_ids[f"insert:{identifiers[0]}"] = CalendarError("500 backend error")

    first = run(fake_calendar, settings=two_day, jobs=[daily_job], now=now)

    assert not first.ok
    assert first.created == 1
    assert len(first.failures) == 1
    assert identifiers[0] in first.failures[0]
    assert identifiers[0] not in fake_calendar.events

    second = run(fake_calendar, settings=two_day, jobs=[daily_job], now=now)

    assert second.ok
    assert second.created == 1
    assert second.unchanged == 1
    assert sorted(fake_calendar.events) == identifiers


def test_a_failed_archive_is_not_counted_as_archived(fake_calendar, two_day, daily_job, now):
    run(fake_calendar, settings=two_day, jobs=[daily_job], now=now)
    doomed = sorted(fake_calendar.events)[0]
    fake_calendar.fail_ids[f"patch:{doomed}"] = CalendarError("403 rate limited")

    report = run(fake_calendar, settings=two_day, jobs=[], now=now)

    assert not report.ok
    assert report.archived == 1
    assert doomed in fake_calendar.events


def test_failed_readback_retries_without_duplicate(two_day, daily_job, now):
    fake = FakeCalendar()
    desired = build_schedule_events([daily_job], profile=PROFILE, settings=two_day, now=now, tz_name="UTC")
    fake.fail_ids[f"get:{desired[0].id}"] = CalendarError("unavailable")
    assert not run(fake, settings=two_day, jobs=[daily_job], now=now).ok
    assert run(fake, settings=two_day, jobs=[daily_job], now=now).ok
    assert len(fake.events) == len(desired)


# ---------------------------------------------------------------------------
# Target verification and blast radius
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"primary": True}, "primary calendar"),
        ({"calendar_id": "primary"}, "primary"),
        ({"calendar_id": "not-an-id"}, "secondary calendar id"),
        ({"access_role": "reader"}, "not writable"),
        ({"access_role": "freeBusyReader"}, "not writable"),
    ],
)
def test_unverified_targets_are_refused_before_any_write(
    two_day, daily_job, now, kwargs, expected
):
    fake = FakeCalendar(**kwargs)

    with pytest.raises(ConfigurationError) as excinfo:
        run(fake, settings=two_day, jobs=[daily_job], now=now)

    assert "not verified" in str(excinfo.value)
    assert fake.write_calls() == []
    assert "list" not in fake.calls


def test_a_verify_read_failure_aborts_without_writing(two_day, daily_job, now):
    fake = FakeCalendar()
    fake.fail_next["verify"] = CalendarError("timed out")

    with pytest.raises(CalendarError):
        run(fake, settings=two_day, jobs=[daily_job], now=now)

    assert fake.write_calls() == []


def test_a_list_read_failure_aborts_without_writing(two_day, daily_job, now):
    fake = FakeCalendar()
    fake.fail_next["list"] = CalendarError("403 permission denied")

    with pytest.raises(CalendarError):
        run(fake, settings=two_day, jobs=[daily_job], now=now)

    assert fake.write_calls() == []


def test_missing_access_role_is_denied(
    two_day, daily_job, now
):
    """calendar.app.created tokens can lack a calendarList entry."""
    fake = FakeCalendar(access_role=None)

    with pytest.raises(ConfigurationError):
        run(fake, settings=two_day, jobs=[daily_job], now=now)
    assert fake.write_calls() == []


def test_dry_run_never_writes(fake_calendar, two_day, daily_job, now):
    report = run(fake_calendar, settings=two_day, jobs=[daily_job], now=now, dry_run=True)

    assert report.dry_run and report.created == 2
    assert fake_calendar.write_calls() == []
    assert fake_calendar.events == {}


def test_another_profiles_events_are_neither_read_nor_pruned(
    fake_calendar, two_day, daily_job, now
):
    foreign_id = event_id("other-profile", KIND_SCHEDULE, "job-z", "2026-09-07T09:00:00+00:00")
    fake_calendar.events[foreign_id] = {
        "id": foreign_id,
        "summary": "⏱ someone else",
        "extendedProperties": {"private": {
            PROP_PROFILE: "other-profile", PROP_KIND: KIND_SCHEDULE, PROP_JOB: "job-z",
        }},
    }

    report = run(fake_calendar, settings=two_day, jobs=[daily_job], now=now)

    assert report.observed == 0
    assert foreign_id in fake_calendar.events
    assert f"delete:{foreign_id}" not in fake_calendar.calls


def test_unrecognised_managed_events_are_left_alone(
    fake_calendar, two_day, daily_job, now
):
    """A future schema's events must not be pruned by an older reconciler."""
    stray = "hcm" + "f" * 29
    fake_calendar.events[stray] = {
        "id": stray,
        "extendedProperties": {"private": {
            PROP_PROFILE: PROFILE, PROP_KIND: "some-future-kind",
        }},
    }

    report = run(fake_calendar, settings=two_day, jobs=[daily_job], now=now)

    assert report.kept_history == 1
    assert stray in fake_calendar.events


def test_written_events_are_private_free_and_guest_less(
    fake_calendar, two_day, daily_job, now
):
    run(fake_calendar, settings=two_day, jobs=[daily_job], now=now)

    for event in fake_calendar.events.values():
        assert event["visibility"] == "private"
        assert event["transparency"] == "transparent"
        assert "attendees" not in event
        assert "conferenceData" not in event
        assert "source" not in event


def test_digest_and_run_events_coexist_for_a_high_frequency_job(
    fake_calendar, settings, now
):
    job = make_job(
        "job-poll",
        name="Inbox poll",
        schedule={"kind": "interval", "minutes": 5, "display": "every 5m"},
        next_run_at=now + timedelta(minutes=2),
    )
    execution = make_execution(
        "exec-poll", "job-poll",
        claimed_at=now - timedelta(minutes=5),
        finished_at=now - timedelta(minutes=5) + timedelta(seconds=12),
    )

    report = run(fake_calendar, settings=replace(settings, horizon_days=1),
                 jobs=[job], executions=[execution], now=now)

    assert report.ok
    assert len(fake_calendar.ids_of_kind(KIND_DIGEST)) == 2
    assert len(fake_calendar.ids_of_kind(KIND_RUN)) == 1
    assert all(fake_calendar.events[k]["extendedProperties"]["private"][PROP_STATUS] == "archived" for k in fake_calendar.ids_of_kind(KIND_SCHEDULE))


def test_external_tombstone_is_reported_without_replacement(two_day, daily_job, now):
    fake = FakeCalendar()
    desired = build_schedule_events([daily_job], profile=PROFILE, settings=two_day, now=now, tz_name="UTC")
    fake.reserved.update(event.id for event in desired)
    report = run(fake, settings=two_day, jobs=[daily_job], now=now)
    assert not report.ok
    assert fake.events == {}
    assert not any(call.startswith("patch:") for call in fake.calls)
