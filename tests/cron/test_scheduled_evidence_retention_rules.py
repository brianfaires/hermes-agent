"""Retention contracts at native ledger lifecycle/read boundaries, with synthetic legacy rows."""

import sqlite3
import subprocess
import sys

import pytest


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    from cron import executions, jobs

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 1)
    with jobs.use_cron_store(tmp_path / "cron"):
        yield executions


def _complete(ledger, job="weekly", source="builtin", instant=None, **outcome):
    row = ledger.create_execution(job, source=source, scheduled_instant=instant)
    assert ledger.mark_execution_running(row["id"]) is not None
    return ledger.finish_execution(row["id"], success=True, **outcome)


def _seed(ledger, rows):
    """Legacy stored values cannot all be produced by today's canonicalizing writer."""
    assert ledger.get_execution("absent") is None  # initialize the real schema
    with sqlite3.connect(ledger.EXECUTIONS_FILE) as conn:
        conn.executemany(
            "INSERT INTO executions "
            "(id,job_id,source,process_id,pid,status,claimed_at,finished_at,scheduled_instant) "
            "VALUES (?,?,?,'legacy',-1,?,?,?,?)",
            [(row["id"], row.get("job", "weekly"), row.get("source", "builtin"),
              row.get("status", "completed"), row.get("claimed", "2026-01-02T00:00:00Z"),
              row.get("finished", "2026-01-03T00:00:00Z"), row.get("instant"))
             for row in rows],
        )


@pytest.mark.parametrize("invalid", [
    None, "", "not-a-time", "2026-01-01", "2026-01-01T12:00:00", "2461042.5",
    "2026-02-30T00:00:00Z", "2026-01-01T00:00:00+24:00", "NaN", "Infinity",
    "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00",
])
def test_invalid_legacy_occurrence_cannot_pin_or_displace(ledger, invalid):
    _seed(ledger, [
        {"id": "scheduled", "instant": "2026-01-01T00:00:00Z"},
        {"id": "invalid", "instant": invalid, "finished": "2026-01-04T00:00:00Z"},
    ])
    newest = _complete(ledger, "noise")
    assert {row["id"] for row in ledger.list_executions()} == {"scheduled", newest["id"]}


@pytest.mark.parametrize(("winner", "loser"), [
    # A late finish cannot replace a newer occurrence.
    ({"instant": "2026-01-02T00:00:00Z"},
     {"instant": "2026-01-01T00:00:00Z", "finished": "2026-01-04T00:00:00Z"}),
    # Offset text order is the reverse of chronological order.
    ({"instant": "2026-01-01T00:00:00-08:00"},
     {"instant": "2026-01-01T07:59:59Z", "finished": "2026-01-04T00:00:00Z"}),
    # SQLite's millisecond resolution and float timestamps would lose these distinctions.
    ({"instant": "2026-01-01T00:00:00.000002Z"},
     {"instant": "2026-01-01T00:00:00.000001Z", "finished": "2026-01-04T00:00:00Z"}),
    ({"instant": "9999-12-31T23:59:59.999999Z"},
     {"instant": "9999-12-31T23:59:59.999998Z", "finished": "2026-01-04T00:00:00Z"}),
    # Equivalent occurrences defer to terminal time, then terminal text.
    ({"instant": "2026-01-01T08:00:00+08:00", "finished": "2026-01-03T00:00:00-08:00"},
     {"instant": "2026-01-01T00:00:00Z", "finished": "2026-01-03T07:00:00Z"}),
    ({"instant": "2026-01-01T08:00:00+08:00", "finished": "2026-01-03T08:00:00Z"},
     {"instant": "2026-01-01T00:00:00Z", "finished": "2026-01-03T00:00:00-08:00"}),
    # Equal terminal times defer to claim time, then claim text, then ID.
    ({"claimed": "2026-01-02T00:00:00-08:00"}, {"claimed": "2026-01-02T07:00:00Z"}),
    ({"claimed": "2026-01-02T08:00:00Z"}, {"claimed": "2026-01-02T00:00:00-08:00"}),
    ({}, {}),
])
def test_occurrence_order_then_existing_terminal_claim_and_id_ties(ledger, winner, loser):
    _seed(ledger, [
        {"id": "z-winner", "instant": "2026-01-01T00:00:00Z", **winner},
        {"id": "a-loser", "instant": "2026-01-01T00:00:00Z", **loser},
    ])
    newest = _complete(ledger, "noise")
    assert {row["id"] for row in ledger.list_executions()} == {"z-winner", newest["id"]}


@pytest.mark.parametrize("mode", ["force", "manual", "ticker", "direct", "builtin-null", "explicit-manual"])
def test_manual_paths_do_not_replace_scheduled_evidence(ledger, mode):
    from cron import jobs
    from cron.scheduler_provider import InProcessCronScheduler

    job = jobs.create_job(prompt="manual retention", schedule="every 7d")
    scheduled = _complete(ledger, job["id"], instant="2026-01-01T00:00:00Z")
    if mode in {"force", "manual", "ticker"}:
        if mode == "ticker":
            jobs.trigger_job(job["id"])
            due = jobs.get_due_jobs()
            assert len(due) == 1 and due[0]["_scheduled_instant"] is None
        claim = InProcessCronScheduler().claim_fire(
            job["id"], force=mode == "force", manual=mode == "manual",
        )
        assert claim is not None
        manual = ledger.finish_execution(claim["execution_id"], success=True)
        assert manual["scheduled_instant"] is None
    else:
        source = {"direct": "direct", "builtin-null": "builtin", "explicit-manual": "manual"}[mode]
        manual = _complete(ledger, job["id"], source=source,
                           instant="2026-01-02T00:00:00Z" if mode == "explicit-manual" else None)
    assert ledger.get_execution(scheduled["id"]) == scheduled
    newest = _complete(ledger, "noise")
    assert {row["id"] for row in ledger.list_executions()} == {scheduled["id"], newest["id"]}
    assert ledger.get_execution(manual["id"]) is None


def test_union_preserves_each_job_source_status_and_active_rows(ledger, monkeypatch):
    monkeypatch.setattr(ledger, "MAX_TERMINAL_EXECUTIONS", 3)
    expected = set()
    for job, source in [("weekly", "builtin"), ("other", "builtin"), ("weekly", "custom-provider")]:
        for status in ("completed", "failed", "unknown", "cancelled", "missed"):
            for day in (1, 2):
                if status == "missed":
                    # The native missed writer uses its own scheduler source.
                    row = ledger.record_missed_occurrences(
                        job + source, count=3, first_instant=f"2026-01-0{day}T00:00:00Z", detail="catch-up off",
                    )
                else:
                    row = ledger.create_execution(job, source=source, scheduled_instant=f"2026-01-0{day}T00:00:00Z")
                    if status == "unknown":
                        assert ledger.give_up_execution(row["id"], reason="owner gave up")
                    else:
                        ledger.finish_execution(row["id"], success=status == "completed", cancelled=status == "cancelled")
                if day == 2:
                    expected.add(row["id"])
    claimed = ledger.create_execution("active", source="builtin")
    running = ledger.create_execution("active", source="builtin", scheduled_instant="2026-01-03T00:00:00Z")
    running = ledger.mark_execution_running(running["id"])
    recent = [_complete(ledger, "noise") for _ in range(4)]
    expected.update(row["id"] for row in recent[-3:])
    expected.update([claimed["id"], running["id"]])
    assert {row["id"] for row in ledger.list_executions(limit=100)} == expected
    assert ledger.get_execution(claimed["id"]) == claimed
    assert ledger.get_execution(running["id"]) == running


@pytest.mark.parametrize("outcome", ["failed", "delivered"])
def test_retained_queued_delivery_settles_without_rewriting_terminal_result(ledger, outcome):
    saved = _complete(ledger, instant="2026-01-01T00:00:00Z", delivery_outcome="queued")
    _complete(ledger, "noise")
    assert ledger.settle_delivery_outcome(saved["id"], outcome)
    assert not ledger.settle_delivery_outcome(saved["id"], "suppressed")
    assert ledger.finish_execution(saved["id"], success=False, error="late writer") is None
    assert ledger.get_execution(saved["id"]) == {**saved, "delivery_outcome": outcome}


def test_profile_local_pins_do_not_displace_same_identity_elsewhere(ledger, monkeypatch, tmp_path):
    monkeypatch.setattr(ledger, "EXECUTIONS_FILE", None)
    saved = []
    for name, instant in [("first", "2026-01-01T00:00:00Z"), ("second", "2026-01-02T00:00:00Z")]:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / name))
        pin = _complete(ledger, instant=instant)
        newest = _complete(ledger, "noise")
        saved.append((name, pin, newest))
    for name, pin, newest in saved:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / name))
        assert {row["id"] for row in ledger.list_executions()} == {pin["id"], newest["id"]}


def test_identity_growth_does_not_require_a_growing_sql_parameter_list(ledger, monkeypatch):
    # Exercise a real SQLite variable limit while more than 1,000 partitions survive.
    connect = sqlite3.connect

    def limited_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 64)
        return conn

    monkeypatch.setattr(sqlite3, "connect", limited_connect)
    _seed(ledger, [
        {"id": f"{generation}-{index}", "job": f"job-{index}",
         "instant": f"2026-01-0{day}T00:00:00Z"}
        for index in range(1001) for generation, day in [("old", 1), ("pin", 2)]
    ])
    _complete(ledger, "noise")
    for index in range(1001):
        records = ledger.list_executions(job_id=f"job-{index}")
        assert [row["id"] for row in records] == [f"pin-{index}"]


@pytest.mark.parametrize("recovery", ["sweep", "worker-death"])
def test_dead_owner_scheduled_evidence_survives_pruning_and_receipt_settlement(ledger, recovery):
    child = subprocess.run(
        [sys.executable, "-c",
         "from cron import executions as e; "
         "r=e.create_execution('weekly',source='custom-provider',"
         "scheduled_instant='2026-01-01T00:00:00Z'); "
         "e.mark_execution_running(r['id']); print(r['id'])"],
        capture_output=True, text=True, check=True, timeout=30,
    )
    execution_id = child.stdout.strip().splitlines()[-1]
    assert ledger.get_execution(execution_id)["status"] == "running"
    if recovery == "sweep":
        assert ledger.recover_interrupted_executions() == 1
    else:
        assert ledger.terminalize_dead_owner(execution_id, reason="observed worker exit")
    unknown = ledger.get_execution(execution_id)
    assert unknown["status"] == "unknown"
    _complete(ledger, "noise")
    assert ledger.get_execution(execution_id) == unknown
    assert ledger.recover_interrupted_executions() == 0
    settled = ledger.recover_receipted_execution(
        execution_id, "weekly", success=True, delivery_outcome="suppressed",
    )
    assert settled["status"] == "completed"
    _complete(ledger, "noise")
    assert ledger.get_execution(execution_id) == settled
    assert ledger.recover_receipted_execution(execution_id, "weekly", success=False) is None


def test_global_window_keeps_original_terminal_order_and_overlapping_pin(ledger, monkeypatch):
    monkeypatch.setattr(ledger, "MAX_TERMINAL_EXECUTIONS", 3)
    _seed(ledger, [
        {"id": "old-pin", "job": "sparse", "instant": "2026-01-01T00:00:00Z"},
        {"id": "textually-newer", "finished": "2026-01-04T07:00:00Z"},
        {"id": "chronologically-newer", "finished": "2026-01-04T00:00:00-08:00"},
        {"id": "overlapping-pin", "instant": "2026-01-02T00:00:00Z", "finished": "2026-01-05T00:00:00Z"},
    ])
    newest = _complete(ledger, "noise")
    assert {row["id"] for row in ledger.list_executions()} == {
        "old-pin", "chronologically-newer", "overlapping-pin", newest["id"],
    }
