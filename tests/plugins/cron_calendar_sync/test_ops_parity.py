"""Contracts for the versioned Ops cron Calendar runner."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import types

import pytest

from hermes_plugins.cron_calendar_sync import calendar_sync as bridge


def job(**overrides):
    value = {
        "id": "job1",
        "name": "My Job",
        "enabled": True,
        "state": "scheduled",
        "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        "__profile": "ops",
    }
    value.update(overrides)
    return value


def load_runner(monkeypatch):
    runner_path = Path(bridge.__file__).resolve().with_name("ops_runner.py")
    monkeypatch.setitem(sys.modules, "google_api", types.ModuleType("google_api"))
    spec = importlib.util.spec_from_file_location("test_versioned_calendar_runner", runner_path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, runner)
    original_home = os.environ.get("HERMES_HOME")
    try:
        spec.loader.exec_module(runner)
    finally:
        if original_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = original_home
    return runner, runner_path


def test_versioned_runner_loads_sibling_engine_and_cli_single_job_does_not_sweep(
    monkeypatch, capsys
):
    runner, runner_path = load_runner(monkeypatch)

    assert bridge.OPS_RUNNER == runner_path
    assert runner.RECONCILER_PATH == runner_path.with_name("reconciler.py")
    monkeypatch.setattr(runner, "reconcile_single_job", lambda *args, **kwargs: {"created": 1})
    monkeypatch.setattr(runner, "sync", lambda **kwargs: (_ for _ in ()).throw(AssertionError("single-job enumerated inventory")))
    monkeypatch.setattr(sys, "argv", [str(runner_path), "--single-job"])
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO('{"operation": "create", "job": {"id": "job1", "__profile": "ops"}}'),
    )

    assert runner.main() == 0
    assert '"created": 1' in capsys.readouterr().out


def test_single_job_cli_forwards_complete_snapshot_without_inventory(monkeypatch, capsys):
    runner, runner_path = load_runner(monkeypatch)
    received = []
    monkeypatch.setattr(
        runner,
        "reconcile_single_job",
        lambda *args, **kwargs: received.append((args, kwargs)) or {"archived": 1},
    )
    monkeypatch.setattr(runner, "sync", lambda **kwargs: (_ for _ in ()).throw(AssertionError("single-job enumerated inventory")))
    snapshot = job(__profile_home="/untrusted/source", marker="preserved")
    monkeypatch.setattr(sys, "argv", [str(runner_path), "--single-job", "--dry-run"])
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({
            "operation": "complete",
            "job": snapshot,
            "output_file": "/managed/output.md",
            "success": True,
            "duration_seconds": 2.5,
        })),
    )

    assert runner.main() == 0
    assert '"archived": 1' in capsys.readouterr().out
    assert received == [(
        (snapshot, "complete"),
        {
            "output_file": "/managed/output.md",
            "success": True,
            "duration_seconds": 2.5,
            "dry_run": True,
        },
    )]


def test_full_sweep_cli_preserves_supported_flags(monkeypatch, capsys):
    runner, runner_path = load_runner(monkeypatch)
    received = []
    monkeypatch.setattr(runner, "sync", lambda **kwargs: received.append(kwargs) or {"unchanged": 1})
    monkeypatch.setattr(sys, "argv", [str(runner_path), "--dry-run", "--skip-output-attachments"])

    assert runner.main() == 0
    assert '"unchanged": 1' in capsys.readouterr().out
    assert received == [{"dry_run": True, "skip_output_attachments": True}]


@pytest.mark.parametrize(
    "override",
    ["--source-home", "--credential-root", "--state-file", "--output-path"],
)
def test_runner_rejects_arbitrary_managed_path_overrides(monkeypatch, override):
    runner, runner_path = load_runner(monkeypatch)
    monkeypatch.setattr(sys, "argv", [str(runner_path), override, "/tmp/unmanaged"])

    with pytest.raises(SystemExit) as excinfo:
        runner.main()

    assert excinfo.value.code == 2


def test_single_job_uses_shared_primitive_once_without_inventory(monkeypatch):
    runner, _ = load_runner(monkeypatch)
    calls = []
    context = object()
    monkeypatch.setattr(runner, "get_service", lambda: object())
    monkeypatch.setattr(runner, "ensure_calendar", lambda service, dry_run: "calendar")
    monkeypatch.setattr(runner, "_build_context", lambda *args: context)
    monkeypatch.setattr(runner, "load_all_jobs", lambda: (_ for _ in ()).throw(AssertionError("single-job enumerated inventory")))
    monkeypatch.setattr(
        runner.reconciler,
        "reconcile_one",
        lambda snapshot, operation, actual_context, **kwargs: calls.append((snapshot, operation, actual_context, kwargs)) or {"archived": 1},
    )

    assert runner.reconcile_single_job(job(), "remove") == {"archived": 1}
    assert len(calls) == 1
    snapshot, operation, actual_context, kwargs = calls[0]
    assert snapshot["id"] == "job1"
    assert operation == "remove"
    assert actual_context is context
    assert kwargs == {"output_file": None, "success": False, "duration_seconds": None}


def test_recovery_sweep_calls_shared_primitive_once_per_inventory_job_then_orphans(monkeypatch):
    runner, _ = load_runner(monkeypatch)
    canonical_jobs = [job(id="default"), job(id="ops")]
    calls = []
    context = object()
    monkeypatch.setattr(runner, "load_all_jobs", lambda: canonical_jobs)
    monkeypatch.setattr(runner, "get_service", lambda: object())
    monkeypatch.setattr(runner, "ensure_calendar", lambda service, dry_run: "calendar")
    monkeypatch.setattr(runner, "live_events_by_job", lambda service, calendar_id: {})
    monkeypatch.setattr(runner, "_build_context", lambda *args: context)
    monkeypatch.setattr(
        runner.reconciler,
        "reconcile_one",
        lambda snapshot, operation, actual_context: calls.append((snapshot["id"], operation, actual_context)) or {"unchanged": 1},
    )
    monkeypatch.setattr(
        runner.reconciler,
        "reconcile_orphans",
        lambda jobs, actual_context: calls.append(("orphans", jobs, actual_context)) or {"archived": 0, "deleted": 0},
    )

    result = runner.sync()

    assert result["unchanged"] == 2
    assert calls == [
        ("default", "update", context),
        ("ops", "update", context),
        ("orphans", canonical_jobs, context),
    ]


def test_recovery_inventory_coalesces_identical_profiles_and_prefers_ops(tmp_path, monkeypatch):
    runner, _ = load_runner(monkeypatch)
    default_home = tmp_path / "default"
    ops_home = tmp_path / "ops"
    identical = job(id="shared", __profile="caller-controlled")
    for home in (default_home, ops_home):
        path = home / "cron" / "jobs.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"jobs": [identical]}))
    monkeypatch.setattr(runner, "PROFILE_HOMES", [("default", default_home), ("ops", ops_home)])

    assert runner.load_all_jobs() == [
        {
            **identical,
            "__profile": "ops",
            "__profile_home": str(ops_home),
        }
    ]


def test_runner_keeps_fixed_google_and_ops_state_boundaries(monkeypatch):
    runner, _ = load_runner(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", "/tmp/attacker-controlled-home")
    captured = []
    monkeypatch.setattr(runner, "ensure_policy_seen", lambda: captured.append("policy"))
    monkeypatch.setattr(
        runner.google_api,
        "build_service",
        lambda *args: captured.append(args) or object(),
        raising=False,
    )

    runner.get_service()
    context = runner._build_context(object(), "calendar", False, {})
    monkeypatch.setattr(runner.reconciler, "CalendarStateLock", lambda path: captured.append(path) or object())
    context.lock()

    assert captured == [
        "policy",
        ("calendar", "v3"),
        runner.STATE_PATH.with_suffix(".lock"),
    ]
    assert runner.GOOGLE_HOME == runner.DEFAULT_HOME
    assert runner.STATE_PATH == runner.OPS_HOME / "state" / "cron_calendar_recurring_sync.json"


def test_runner_rejects_absolute_job_id_output_escape(tmp_path, monkeypatch):
    runner, _ = load_runner(monkeypatch)
    output = tmp_path / "escaped.md"
    output.write_text("## Response\nnot managed")
    malicious_job = job(id=str(tmp_path))

    assert runner._managed_output_file(str(output), malicious_job) is None


def test_single_job_rejects_noncanonical_job_id_before_calendar_access(monkeypatch):
    runner, _ = load_runner(monkeypatch)
    monkeypatch.setattr(
        runner,
        "get_service",
        lambda: (_ for _ in ()).throw(AssertionError("invalid job reached Calendar")),
    )

    with pytest.raises(ValueError, match="invalid cron job id"):
        runner.reconcile_single_job(job(id="../../escaped"), "update")


def test_recovery_sweep_redacts_secret_assignments_from_failures(monkeypatch):
    runner, _ = load_runner(monkeypatch)
    context = object()
    monkeypatch.setattr(runner, "load_all_jobs", lambda: [job()])
    monkeypatch.setattr(runner, "get_service", lambda: object())
    monkeypatch.setattr(runner, "ensure_calendar", lambda service, dry_run: "calendar")
    monkeypatch.setattr(runner, "live_events_by_job", lambda service, calendar_id: {})
    monkeypatch.setattr(runner, "_build_context", lambda *args: context)
    monkeypatch.setattr(
        runner.reconciler,
        "reconcile_one",
        lambda *args: (_ for _ in ()).throw(
            RuntimeError("GOOGLE_CLIENT_SECRET=calendar-secret-value")
        ),
    )
    monkeypatch.setattr(
        runner.reconciler,
        "reconcile_orphans",
        lambda jobs, actual_context: {"archived": 0, "deleted": 0},
    )

    result = runner.sync()

    assert "calendar-secret-value" not in json.dumps(result)
    assert "GOOGLE_CLIENT_SECRET: REDACTED" in result["error_messages"][0]


def test_recovery_sweep_redacts_quoted_json_secret_failures(monkeypatch):
    runner, _ = load_runner(monkeypatch)
    context = object()
    monkeypatch.setattr(runner, "load_all_jobs", lambda: [job()])
    monkeypatch.setattr(runner, "get_service", lambda: object())
    monkeypatch.setattr(runner, "ensure_calendar", lambda service, dry_run: "calendar")
    monkeypatch.setattr(runner, "live_events_by_job", lambda service, calendar_id: {})
    monkeypatch.setattr(runner, "_build_context", lambda *args: context)
    monkeypatch.setattr(
        runner.reconciler,
        "reconcile_one",
        lambda *args: (_ for _ in ()).throw(
            RuntimeError('{"access_token": "json-secret-value"}')
        ),
    )
    monkeypatch.setattr(
        runner.reconciler,
        "reconcile_orphans",
        lambda jobs, actual_context: {"archived": 0, "deleted": 0},
    )

    result = runner.sync()

    serialized = json.dumps(result)
    assert "json-secret-value" not in serialized
    assert '"access_token": "REDACTED"' in result["error_messages"][0]


def test_single_job_cli_redacts_secret_assignments_from_failures(
    monkeypatch, capsys
):
    runner, runner_path = load_runner(monkeypatch)
    monkeypatch.setattr(
        runner,
        "reconcile_single_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("OPS_ACCESS_TOKEN=single-job-secret-value")
        ),
    )
    monkeypatch.setattr(sys, "argv", [str(runner_path), "--single-job"])
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO('{"operation": "update", "job": {"id": "job1"}}'),
    )

    assert runner.main() == 1
    output = capsys.readouterr().out
    assert "single-job-secret-value" not in output
    assert "OPS_ACCESS_TOKEN: REDACTED" in output


def test_single_job_cli_redacts_quoted_dict_secret_failures(
    monkeypatch, capsys
):
    runner, runner_path = load_runner(monkeypatch)
    monkeypatch.setattr(
        runner,
        "reconcile_single_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("{'client_secret': 'dict-secret-value'}")
        ),
    )
    monkeypatch.setattr(sys, "argv", [str(runner_path), "--single-job"])
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO('{"operation": "update", "job": {"id": "job1"}}'),
    )

    assert runner.main() == 1
    output = capsys.readouterr().out
    assert "dict-secret-value" not in output
    assert "'client_secret': 'REDACTED'" in output


@pytest.mark.parametrize("stale_kind", ["not-found", "gone", "not-found-class"])
def test_stale_tracked_output_instance_is_readopted_without_error(
    tmp_path, monkeypatch, stale_kind
):
    runner, _ = load_runner(monkeypatch)
    output = tmp_path / "2026-01-01_09-00-00.md"
    output.write_text("## Response\nok")
    state = {
        "run_outputs": {
            "job1": {
                output.name: {"instance_id": "stale-instance", "render_version": 1}
            }
        }
    }
    calls = []

    class Gone(Exception):
        class resp:
            status = 410

    class Missing(Exception):
        class resp:
            status = 404

    CalendarNotFoundError = type("CalendarNotFoundError", (Exception,), {})
    stale_error = {
        "not-found": Missing("tracked instance is missing"),
        "gone": Gone("tracked instance is gone"),
        "not-found-class": CalendarNotFoundError("tracked instance was not found"),
    }[stale_kind]

    class Request:
        def __init__(self, result=None, error=None):
            self.result = result
            self.error = error

        def execute(self):
            if self.error:
                raise self.error
            return self.result

    class Events:
        def get(self, **kwargs):
            calls.append(("get", kwargs["eventId"]))
            return Request(error=stale_error)

        def instances(self, **kwargs):
            calls.append(("instances", kwargs["eventId"]))
            return Request(
                result={
                    "items": [
                        {
                            "id": "current-instance",
                            "description": "existing",
                            "start": {"dateTime": "2026-01-01T09:00:00-08:00"},
                        }
                    ]
                }
            )

        def patch(self, **kwargs):
            calls.append(("patch", kwargs["eventId"]))
            return Request(result={})

    service = types.SimpleNamespace(events=lambda: Events())

    attached, messages = runner.attach_output_file_to_instance(
        service, "calendar", "series", job(), state, output, False
    )

    assert attached == 1
    assert messages == []
    assert state["run_outputs"]["job1"][output.name]["instance_id"] == "current-instance"
    assert calls == [
        ("get", "stale-instance"),
        ("instances", "series"),
        ("patch", "current-instance"),
    ]


def test_transient_tracked_output_instance_failure_aborts_before_fallback(
    tmp_path, monkeypatch
):
    runner, _ = load_runner(monkeypatch)
    output = tmp_path / "2026-01-01_09-00-00.md"
    output.write_text("## Response\nok")
    state = {
        "run_outputs": {
            "job1": {
                output.name: {"instance_id": "tracked-instance", "render_version": 1}
            }
        }
    }
    calls = []

    class Request:
        def execute(self):
            raise RuntimeError("temporary Calendar failure")

    class Events:
        def get(self, **kwargs):
            calls.append(("get", kwargs["eventId"]))
            return Request()

        def instances(self, **kwargs):
            calls.append(("instances", kwargs["eventId"]))
            raise AssertionError("transient tracked-instance failure reached fallback")

    service = types.SimpleNamespace(events=lambda: Events())

    with pytest.raises(RuntimeError, match="temporary Calendar failure"):
        runner.attach_output_file_to_instance(
            service, "calendar", "series", job(), state, output, False
        )

    assert calls == [("get", "tracked-instance")]
    assert state["run_outputs"]["job1"][output.name] == {
        "instance_id": "tracked-instance",
        "render_version": 1,
    }
