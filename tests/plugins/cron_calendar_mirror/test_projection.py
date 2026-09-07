"""Schedule projection: phase, horizon, lifecycle states, and identity.

These exercise the real ``cron.jobs`` schedule maths — no cron behaviour is
stubbed — so a change to the scheduler's own next-run computation shows up here.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from plugins.cron_calendar_mirror import mirror
from plugins.cron_calendar_mirror.mirror import (
    KIND_DIGEST,
    KIND_SCHEDULE,
    PROP_JOB,
    PROP_OCCURRENCE,
    Settings,
    build_schedule_events,
    event_id,
    project_occurrences,
)

from .conftest import UTC, make_job

PROFILE = "ops"


def _events(jobs, *, now, settings=None, tz_name="UTC"):
    return build_schedule_events(
        jobs,
        profile=PROFILE,
        settings=settings or Settings(calendar_id="c@group.calendar.google.com"),
        now=now,
        tz_name=tz_name,
    )


# ---------------------------------------------------------------------------
# Phase: the t_0cfea844 regression
# ---------------------------------------------------------------------------

def test_interval_projection_is_anchored_on_next_run_at_not_created_at(now):
    """Occurrences must land on the scheduler's phase, not the job's birthday.

    The legacy plugin anchored its recurrence on ``created_at``, so a job
    created at :00 but actually next firing at :17 was mirrored 17 minutes out
    of phase for its whole life.
    """
    job = make_job(
        "job-interval",
        schedule={"kind": "interval", "minutes": 90, "display": "every 90m"},
        created_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
        next_run_at=now + timedelta(minutes=17),
    )

    occurrences = project_occurrences(
        job, now=now, horizon_end=now + timedelta(days=1), limit=10
    )

    assert occurrences[0] == now + timedelta(minutes=17)
    assert occurrences[1] == now + timedelta(minutes=17 + 90)
    # Every occurrence sits on the next_run_at phase, never the created_at one.
    anchor = now + timedelta(minutes=17)
    assert all(
        (moment - anchor).total_seconds() % (90 * 60) == 0 for moment in occurrences
    )
    assert now not in occurrences


def test_stale_next_run_at_is_fast_forwarded_through_the_scheduler(now):
    """A scheduler that has been down does not produce past calendar events."""
    job = make_job(
        "job-stale",
        schedule={"kind": "cron", "expr": "0 * * * *", "display": "hourly"},
        next_run_at=now - timedelta(days=3),
    )

    occurrences = project_occurrences(
        job, now=now, horizon_end=now + timedelta(hours=5), limit=10
    )

    assert occurrences, "a recoverable recurring job should still be mirrored"
    assert all(moment >= now - timedelta(minutes=2) for moment in occurrences)


# ---------------------------------------------------------------------------
# Schedule kinds
# ---------------------------------------------------------------------------

def test_one_shot_projects_exactly_one_occurrence(now):
    job = make_job(
        "job-once",
        schedule={"kind": "once", "run_at": (now + timedelta(hours=3)).isoformat(),
                  "display": "once at 2026-09-06 15:00"},
        next_run_at=now + timedelta(hours=3),
        repeat_times=1,
    )

    occurrences = project_occurrences(
        job, now=now, horizon_end=now + timedelta(days=7), limit=32
    )

    assert occurrences == [now + timedelta(hours=3)]


def test_cron_projection_respects_the_horizon(now):
    job = make_job(
        "job-daily",
        schedule={"kind": "cron", "expr": "0 9 * * *", "display": "every day at 9am"},
        next_run_at=datetime(2026, 9, 7, 9, 0, tzinfo=UTC),
    )

    events = _events([job], now=now, settings=Settings(
        calendar_id="c@group.calendar.google.com", horizon_days=3
    ))

    assert len(events) == 3
    assert all(event.kind == KIND_SCHEDULE for event in events)
    starts = [event.start["dateTime"] for event in events]
    assert starts == sorted(starts)


def test_repeat_budget_caps_projected_occurrences(now):
    job = make_job(
        "job-bounded",
        schedule={"kind": "cron", "expr": "0 * * * *", "display": "hourly"},
        next_run_at=now + timedelta(minutes=30),
        repeat_times=5,
        repeat_completed=3,
    )

    occurrences = project_occurrences(
        job, now=now, horizon_end=now + timedelta(days=2), limit=32
    )

    assert len(occurrences) == 2


def test_timezone_name_is_attached_to_timed_events(now):
    job = make_job(
        "job-tz",
        schedule={"kind": "cron", "expr": "0 9 * * *", "display": "every day at 9am"},
        next_run_at=datetime(2026, 9, 7, 9, 0, tzinfo=UTC),
    )

    events = _events([job], now=now, tz_name="America/Los_Angeles",
                     settings=Settings(calendar_id="c@group.calendar.google.com",
                                       horizon_days=1))

    assert events[0].start["timeZone"] == "America/Los_Angeles"
    assert events[0].end["timeZone"] == "America/Los_Angeles"
    # The instant itself is unambiguous regardless of the display zone.
    assert events[0].start["dateTime"].endswith("+00:00")


def test_cron_occurrences_follow_the_configured_hermes_zone(monkeypatch, now):
    """A daily 9am job means 9am *local*, and the mirror must agree."""
    import hermes_time

    monkeypatch.setenv("HERMES_TIMEZONE", "America/New_York")
    hermes_time.reset_cache()

    job = make_job(
        "job-ny",
        schedule={"kind": "cron", "expr": "0 9 * * *", "display": "every day at 9am"},
        # 2026-09-07 09:00 in New York (EDT, UTC-4) is 13:00Z.
        next_run_at=datetime(2026, 9, 7, 13, 0, tzinfo=UTC),
    )

    occurrences = project_occurrences(
        job, now=now, horizon_end=now + timedelta(days=4), limit=32
    )

    assert len(occurrences) == 3
    assert all(moment.astimezone(hermes_time.get_timezone()).hour == 9
               for moment in occurrences)
    assert [moment.astimezone(UTC).hour for moment in occurrences] == [13, 13, 13]


def test_timezone_is_omitted_when_hermes_has_no_configured_zone(now):
    job = make_job(
        "job-no-tz",
        schedule={"kind": "cron", "expr": "0 9 * * *", "display": "daily"},
        next_run_at=datetime(2026, 9, 7, 9, 0, tzinfo=UTC),
    )

    events = _events([job], now=now, tz_name=None,
                     settings=Settings(calendar_id="c@group.calendar.google.com",
                                       horizon_days=1))

    assert "timeZone" not in events[0].start


# ---------------------------------------------------------------------------
# Lifecycle states
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"enabled": False, "state": "paused",
                      "paused_at": "2026-09-05T10:00:00+00:00"}, id="paused"),
        pytest.param({"state": "completed"}, id="completed-one-shot"),
        pytest.param({"state": "error"}, id="errored"),
        pytest.param({"repeat_times": 3, "repeat_completed": 3}, id="repeat-exhausted"),
    ],
)
def test_non_runnable_and_ended_jobs_project_nothing(now, kwargs):
    """An ended or paused series must never be adopted onto the calendar."""
    job = make_job(
        "job-ended",
        schedule={"kind": "cron", "expr": "0 * * * *", "display": "hourly"},
        next_run_at=now + timedelta(minutes=30),
        **kwargs,
    )

    assert project_occurrences(
        job, now=now, horizon_end=now + timedelta(days=1), limit=32
    ) == []
    assert _events([job], now=now) == []


def test_resuming_a_paused_job_restores_the_same_event_ids(now):
    """Pause then resume must reproduce identical ids, not a parallel series."""
    live = make_job(
        "job-resume",
        schedule={"kind": "cron", "expr": "0 * * * *", "display": "hourly"},
        next_run_at=now + timedelta(minutes=30),
    )
    paused = dict(live, enabled=False, state="paused", paused_at="2026-09-06T11:00:00+00:00")

    before = [event.id for event in _events([live], now=now)]
    assert _events([paused], now=now) == []
    after = [event.id for event in _events([live], now=now)]

    assert before and before == after


def test_job_without_next_run_at_is_skipped(now):
    job = make_job("job-null", next_run_at=None)

    assert _events([job], now=now) == []


# ---------------------------------------------------------------------------
# High-frequency digest
# ---------------------------------------------------------------------------

def test_high_frequency_job_collapses_to_one_all_day_event_per_day(now):
    job = make_job(
        "job-fast",
        name="Inbox poll",
        schedule={"kind": "interval", "minutes": 5, "display": "every 5m"},
        next_run_at=now + timedelta(minutes=1),
    )

    events = _events([job], now=now, settings=Settings(
        calendar_id="c@group.calendar.google.com", horizon_days=3
    ))

    assert len(events) == 4  # today plus the three horizon days
    assert {event.kind for event in events} == {KIND_DIGEST}
    assert all("date" in event.start and "dateTime" not in event.start for event in events)
    assert all(event.end["date"] > event.start["date"] for event in events)
    days = [event.start["date"] for event in events]
    assert len(set(days)) == len(days)


def test_hourly_job_is_not_collapsed_into_a_digest(now):
    job = make_job(
        "job-hourly",
        schedule={"kind": "interval", "minutes": 60, "display": "every 60m"},
        next_run_at=now + timedelta(minutes=10),
    )

    events = _events([job], now=now, settings=Settings(
        calendar_id="c@group.calendar.google.com", horizon_days=1
    ))

    assert {event.kind for event in events} == {KIND_SCHEDULE}


def test_high_frequency_digest_is_bounded_by_the_horizon(now):
    """A one-minute job must not enumerate thousands of events."""
    job = make_job(
        "job-minute",
        schedule={"kind": "interval", "minutes": 1, "display": "every 1m"},
        next_run_at=now + timedelta(seconds=30),
    )

    events = _events([job], now=now, settings=Settings(
        calendar_id="c@group.calendar.google.com", horizon_days=7
    ))

    assert len(events) == 8


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_event_ids_are_google_legal_and_deterministic():
    first = event_id("ops", KIND_SCHEDULE, "job-1", "2026-09-06T12:00:00+00:00")
    second = event_id("ops", KIND_SCHEDULE, "job-1", "2026-09-06T12:00:00+00:00")

    assert first == second
    assert 5 <= len(first) <= 1024
    assert set(first) <= set("abcdefghijklmnopqrstuv0123456789")


def test_event_ids_separate_profiles_jobs_kinds_and_occurrences():
    base = ("ops", KIND_SCHEDULE, "job-1", "2026-09-06T12:00:00+00:00")
    variants = {
        event_id(*base),
        event_id("default", *base[1:]),
        event_id(base[0], KIND_DIGEST, *base[2:]),
        event_id(*base[:2], "job-2", base[3]),
        event_id(*base[:3], "2026-09-06T13:00:00+00:00"),
    }

    assert len(variants) == 5


def test_occurrence_key_is_utc_normalised(now):
    """The same instant expressed in two zones must map to one event id."""
    from datetime import timezone

    utc_job = make_job("job-x", next_run_at=datetime(2026, 9, 7, 16, 0, tzinfo=UTC))
    offset_job = make_job(
        "job-x",
        next_run_at=datetime(2026, 9, 7, 18, 0, tzinfo=timezone(timedelta(hours=2))),
    )
    opts = dict(now=now, settings=Settings(
        calendar_id="c@group.calendar.google.com", horizon_days=2))

    utc_ids = [event.id for event in _events([utc_job], **opts)]
    offset_ids = [event.id for event in _events([offset_job], **opts)]

    assert utc_ids == offset_ids


# ---------------------------------------------------------------------------
# Content hygiene
# ---------------------------------------------------------------------------

def test_projected_events_never_carry_the_job_prompt(now):
    job = make_job(
        "job-secret",
        next_run_at=now + timedelta(hours=1),
    )
    job["prompt"] = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA secret payload"

    events = _events([job], now=now, settings=Settings(
        calendar_id="c@group.calendar.google.com", horizon_days=1))

    blob = "\n".join(event.description + event.summary for event in events)
    assert "secret payload" not in blob
    assert "ghp_" not in blob
    assert PROP_JOB in events[0].private_props
    assert events[0].private_props[PROP_OCCURRENCE]


def test_patch_body_never_carries_a_summary(now):
    """User-edited titles must survive every update."""
    job = make_job("job-title", next_run_at=now + timedelta(hours=1))

    event = _events([job], now=now, settings=Settings(
        calendar_id="c@group.calendar.google.com", horizon_days=1))[0]

    assert "summary" not in event.patch_body()
    assert event.insert_body()["summary"].endswith(job["name"])


def test_redaction_failure_falls_back_closed(monkeypatch):
    """If redaction is unavailable we publish a placeholder, never raw text."""
    import agent.redact as redact_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("redaction offline")

    monkeypatch.setattr(redact_module, "redact_sensitive_text", boom)

    assert mirror.redact("token=abcdef") == "[redaction unavailable]"


def test_long_interval_outage_preserves_phase(now):
    anchor = now - timedelta(days=365, minutes=17)
    job = make_job("old", schedule={"kind": "interval", "minutes": 90}, next_run_at=anchor)
    times = project_occurrences(job, now=now, horizon_end=now + timedelta(days=1), limit=10)
    assert times
    assert all((t - anchor).total_seconds() % (90 * 60) == 0 for t in times)


@pytest.mark.parametrize("anchor", ["2026-03-07T09:00:00-08:00", "2026-10-31T09:00:00-07:00"])
def test_projection_follows_scheduler_across_dst(monkeypatch, anchor):
    import hermes_time
    from cron.jobs import compute_next_run

    monkeypatch.setenv("HERMES_TIMEZONE", "America/Los_Angeles")
    hermes_time.reset_cache()
    start = datetime.fromisoformat(anchor)
    job = make_job("dst", schedule={"kind": "cron", "expr": "0 9 * * *"}, next_run_at=start)
    times = project_occurrences(job, now=start, horizon_end=start + timedelta(days=4), limit=4)
    expected = [start]
    for _ in range(3):
        expected.append(datetime.fromisoformat(compute_next_run(job["schedule"], expected[-1].isoformat())))
    assert times == expected
    assert times[0].utcoffset() != times[-1].utcoffset()


def test_finite_high_frequency_job_does_not_forecast_days_after_it_ends(now):
    job = make_job("finite", schedule={"kind": "interval", "minutes": 5}, next_run_at=now, repeat_times=1)
    events = _events([job], now=now)
    assert len(events) == 1
    assert events[0].kind == KIND_SCHEDULE
