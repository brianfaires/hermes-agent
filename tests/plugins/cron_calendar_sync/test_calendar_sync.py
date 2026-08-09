"""Contract tests for the shared cron Calendar reconciliation path."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from hermes_plugins.cron_calendar_sync import calendar_sync as bridge
from hermes_plugins.cron_calendar_sync import reconciler


class Gone(Exception):
    class resp:
        status = 410


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


def context(state=None, *, events=None):
    events = events if events is not None else {}
    calls = {"created": [], "patched": [], "archived": [], "attached": [], "completion": []}

    def get(event_id):
        if event_id == "gone":
            raise Gone()
        return events[event_id]

    def create(body):
        event_id = f"event{len(calls['created']) + 1}"
        events[event_id] = dict(body)
        calls["created"].append(event_id)
        return event_id

    def patch(event_id, body):
        calls["patched"].append((event_id, dict(body)))
        events.setdefault(event_id, {}).update(body)

    ctx = reconciler.ReconcileContext(
        state=state if state is not None else {"events": {}},
        dry_run=False,
        calendar_id="calendar",
        should_include=lambda item: bool(item.get("enabled")) and item.get("state") == "scheduled",
        event_for_job=lambda item: {"summary": "editable", "description": item["name"], "start": {"dateTime": "2026-01-01T09:00:00Z"}, "end": {"dateTime": "2026-01-01T09:05:00Z"}},
        signature=lambda body: body["description"],
        get_event=get,
        create_event=create,
        patch_event=patch,
        adopt_event_id=lambda job_id, tracked: next((event_id for event_id in events if event_id not in tracked), None),
        archive_event=lambda event_id, reason: calls["archived"].append((event_id, reason)),
        attach_output=lambda output, item: calls["attached"].append((output, item["id"])) or 1,
        record_completion=lambda item, success, duration: calls["completion"].append((item["id"], success, duration)),
        now=lambda: datetime(2026, 1, 1).isoformat(),
    )
    return ctx, calls


def test_create_and_sweep_use_the_same_primitive(monkeypatch):
    ctx, calls = context()
    result = reconciler.reconcile_one(job(), "create", ctx)
    assert result == {"created": 1, "adopted": 0}
    assert ctx.state["events"]["job1"]["event_id"] == "event1"
    assert reconciler.reconcile_orphans([], ctx) == {"archived": 1, "deleted": 0}
    assert calls["archived"] == [("event1", "cron is no longer active")]


def test_engine_locks_loads_and_saves_the_same_state_transaction():
    operations = []
    stored = {"events": {}}
    ctx, _ = context(state={"wrong": "state"})

    class Lock:
        def __enter__(self):
            operations.append("lock")

        def __exit__(self, *_):
            operations.append("unlock")

    ctx.lock = Lock
    ctx.load_state = lambda: operations.append("load") or stored
    ctx.save_state = lambda state: operations.append("save")
    assert reconciler.reconcile_one(job(), "create", ctx) == {"created": 1, "adopted": 0}
    assert operations == ["lock", "load", "save", "unlock"]
    assert stored["events"]["job1"]["event_id"] == "event1"


def test_transient_mutation_does_not_save_state():
    stored = {"events": {"job1": {"event_id": "event1", "signature": "old"}}}
    ctx, _ = context(state=stored, events={"event1": {}})
    ctx.load_state = lambda: stored
    saves = []
    ctx.save_state = saves.append
    ctx.patch_event = lambda *_: False
    with pytest.raises(RuntimeError, match="Calendar patch"):
        reconciler.reconcile_one(job(name="Changed"), "update", ctx)
    assert saves == []
    assert stored["events"]["job1"]["signature"] == "old"


def test_adoption_and_manual_title_preservation():
    live = {"legacy": {"summary": "Brian's title"}}
    ctx, calls = context(events=live)
    assert reconciler.reconcile_one(job(), "create", ctx) == {"created": 0, "adopted": 1}
    assert calls["patched"][0][0] == "legacy"
    assert "summary" not in calls["patched"][0][1]


def test_adoption_archives_every_additional_live_series():
    live = {
        "legacy-A": {"summary": "Brian's title"},
        "duplicate-B": {"summary": "Stale duplicate"},
    }
    ctx, calls = context(events=live)
    ctx.untracked_event_ids = lambda job_id, tracked: [
        event_id for event_id in live if event_id not in tracked
    ]

    assert reconciler.reconcile_one(job(), "create", ctx) == {
        "created": 0,
        "adopted": 1,
        "duplicates_archived": 1,
    }
    assert calls["patched"][0][0] == "legacy-A"
    assert calls["archived"] == [("duplicate-B", "duplicate cron series")]


@pytest.mark.parametrize(
    ("signature", "expected_result"),
    [
        ("old", {"updated": 1, "duplicates_archived": 1}),
        ("My Job", {"unchanged": 1, "duplicates_archived": 1}),
    ],
)
def test_tracked_canonical_archives_untracked_live_duplicates(signature, expected_result):
    state = {"events": {"job1": {"event_id": "canonical", "signature": signature}}}
    live = {
        "canonical": {"summary": "Brian's title"},
        "duplicate-B": {"summary": "Stale duplicate"},
    }
    ctx, calls = context(state=state, events=live)
    ctx.untracked_event_ids = lambda job_id, tracked: [
        event_id for event_id in live if event_id not in tracked
    ]

    assert reconciler.reconcile_one(job(), "update", ctx) == expected_result
    assert calls["archived"] == [("duplicate-B", "duplicate cron series")]


def test_adoption_skips_stale_candidate_and_archives_stale_duplicate_with_save():
    stored = {"events": {}}
    live = {
        "stale-A": {"summary": "Gone"},
        "legacy-B": {"summary": "Brian's title"},
        "stale-duplicate-C": {"summary": "Gone duplicate"},
    }
    ctx, calls = context(state={"wrong": "state"}, events=live)
    ctx.load_state = lambda: stored
    saves = []
    ctx.save_state = saves.append
    ctx.untracked_event_ids = lambda job_id, tracked: list(live)

    def patch(event_id, body):
        if event_id == "stale-A":
            raise Gone()
        calls["patched"].append((event_id, dict(body)))
        live.setdefault(event_id, {}).update(body)

    def archive(event_id, reason):
        if event_id == "stale-duplicate-C":
            raise Gone()
        calls["archived"].append((event_id, reason))

    ctx.patch_event = patch
    ctx.archive_event = archive

    assert reconciler.reconcile_one(job(), "create", ctx) == {"created": 0, "adopted": 1}
    assert calls["created"] == []
    assert calls["patched"][0][0] == "legacy-B"
    assert calls["archived"] == []
    assert stored["events"]["job1"]["event_id"] == "legacy-B"
    assert saves == [stored]


def test_adoption_creates_only_after_every_candidate_is_stale():
    stored = {"events": {}}
    live = {
        "stale-A": {"summary": "Gone"},
        "stale-B": {"summary": "Gone"},
    }
    ctx, calls = context(state={"wrong": "state"}, events=live)
    ctx.load_state = lambda: stored
    saves = []
    ctx.save_state = saves.append
    ctx.untracked_event_ids = lambda job_id, tracked: list(live)
    ctx.patch_event = lambda event_id, body: (_ for _ in ()).throw(Gone())

    assert reconciler.reconcile_one(job(), "create", ctx) == {"created": 1, "adopted": 0}
    assert calls["created"] == ["event1"]
    assert stored["events"]["job1"]["event_id"] == "event1"
    assert saves == [stored]


def test_transient_duplicate_archive_failure_does_not_save_state():
    stored = {"events": {"job1": {"event_id": "canonical", "signature": "My Job"}}}
    live = {
        "canonical": {"summary": "Brian's title"},
        "duplicate-B": {"summary": "Stale duplicate"},
    }
    ctx, _ = context(state={"wrong": "state"}, events=live)
    ctx.load_state = lambda: stored
    saves = []
    ctx.save_state = saves.append
    ctx.untracked_event_ids = lambda job_id, tracked: [
        event_id for event_id in live if event_id not in tracked
    ]
    ctx.archive_event = lambda event_id, reason: (_ for _ in ()).throw(
        RuntimeError("archive failed")
    )

    with pytest.raises(RuntimeError, match="archive failed"):
        reconciler.reconcile_one(job(), "update", ctx)

    assert saves == []
    assert stored["events"]["job1"]["signature"] == "My Job"


def test_transition_to_nonrecurring_explicitly_clears_recurrence():
    state = {"events": {"job1": {"event_id": "event1", "signature": "old"}}}
    ctx, calls = context(
        state,
        events={"event1": {"recurrence": ["RRULE:FREQ=DAILY"]}},
    )

    reconciler.reconcile_one(job(schedule={"kind": "once"}), "update", ctx)

    assert calls["patched"] == [
        (
            "event1",
            {
                "description": "My Job",
                "start": {"dateTime": "2026-01-01T09:00:00Z"},
                "end": {"dateTime": "2026-01-01T09:05:00Z"},
                "recurrence": [],
            },
        )
    ]


def test_missing_event_is_recreated_and_remove_archives_not_deletes():
    state = {"events": {"job1": {"event_id": "gone", "signature": "stale"}}}
    ctx, calls = context(state)
    assert reconciler.reconcile_one(job(), "update", ctx)["created"] == 1
    result = reconciler.reconcile_one(job(), "remove", ctx)
    assert result == {"archived": 1}
    assert calls["archived"] == [("event1", "cron is no longer active")]


def test_one_shot_completion_attaches_before_archive():
    ctx, calls = context()
    order = []
    ctx.attach_output = lambda output, item: order.append(("attach", output, item["id"])) or 1
    ctx.record_completion = lambda item, success, duration: order.append(("completion", success, duration))
    ctx.archive_event = lambda event_id, reason: order.append(("archive", event_id, reason))
    reconciler.reconcile_one(job(schedule={"kind": "once"}), "create", ctx)
    result = reconciler.reconcile_one(job(schedule={"kind": "once"}), "complete", ctx, output_file="/managed/output.md", success=True, duration_seconds=1.0)
    assert result == {"archived": 1}
    assert order == [
        ("completion", True, 1.0),
        ("attach", "/managed/output.md", "job1"),
        ("archive", "event1", "cron is no longer active"),
    ]


def test_one_shot_remove_defers_until_completion_attaches_output():
    ctx, calls = context()
    one_shot = job(schedule={"kind": "once"})
    reconciler.reconcile_one(one_shot, "create", ctx)
    assert reconciler.reconcile_one(one_shot, "remove", ctx) == {"deferred": 1}
    assert calls["archived"] == []
    assert reconciler.reconcile_one(one_shot, "complete", ctx, output_file="/managed/output.md") == {"archived": 1}
    assert calls["attached"] == [("/managed/output.md", "job1")]
    assert calls["archived"] == [("event1", "cron is no longer active")]


def test_orphan_sweep_does_not_archive_pending_one_shot_completion():
    ctx, calls = context()
    one_shot = job(schedule={"kind": "once"})
    reconciler.reconcile_one(one_shot, "create", ctx)
    reconciler.reconcile_one(one_shot, "remove", ctx)
    assert reconciler.reconcile_orphans([], ctx) == {"archived": 0, "deleted": 0}
    assert calls["archived"] == []


def test_second_orphan_sweep_archives_one_shot_when_completion_never_arrives():
    ctx, calls = context()
    one_shot = job(schedule={"kind": "once"})
    reconciler.reconcile_one(one_shot, "create", ctx)
    reconciler.reconcile_one(one_shot, "remove", ctx)

    assert reconciler.reconcile_orphans([], ctx) == {"archived": 0, "deleted": 0}
    assert reconciler.reconcile_orphans([], ctx) == {"archived": 1, "deleted": 0}
    assert calls["archived"] == [("event1", "cron is no longer active")]
    assert "job1" not in ctx.state.get("pending_one_shot_removals", {})


def test_non_one_shot_completion_records_duration_and_attaches():
    ctx, calls = context()
    reconciler.reconcile_one(job(), "create", ctx)
    assert reconciler.reconcile_one(job(), "complete", ctx, output_file="/managed/output.md", success=True, duration_seconds=2.0) == {"output_attached": 1}
    assert calls["completion"] == [("job1", True, 2.0)]


def test_recurring_completion_attaches_output_before_failed_duration_update():
    ctx, calls = context()
    reconciler.reconcile_one(job(), "create", ctx)
    ctx.record_completion = lambda *_: (_ for _ in ()).throw(
        RuntimeError("duration patch failed")
    )

    with pytest.raises(RuntimeError, match="duration patch failed"):
        reconciler.reconcile_one(
            job(),
            "complete",
            ctx,
            output_file="/managed/output.md",
            success=True,
            duration_seconds=2.0,
        )

    assert calls["attached"] == [("/managed/output.md", "job1")]


def test_bridge_passes_deleted_remove_snapshot_and_rejects_unmanaged_output(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(bridge, "_enabled", lambda: True)
    monkeypatch.setattr(bridge, "OPS_RUNNER", tmp_path / "runner.py")
    monkeypatch.setattr(bridge, "MANAGED_HOMES", {tmp_path})
    monkeypatch.setattr(bridge, "MANAGED_PROFILES", {tmp_path: "ops"})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    bridge.OPS_RUNNER.write_text("# runner")
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: captured.update(kwargs)
        or bridge.subprocess.CompletedProcess(args, 0),
    )
    bridge.on_remove(job())
    assert __import__("json").loads(captured["input"])["operation"] == "remove"
    assert __import__("json").loads(captured["input"])["job"]["id"] == "job1"
    assert __import__("json").loads(captured["input"])["job"]["__profile"] == "ops"
    assert bridge._safe_output_file("/tmp/not-managed.md", job()) is None


@pytest.mark.parametrize(
    ("handler", "operation"),
    [(bridge.on_create, "create"), (bridge.on_update, "update")],
)
def test_bridge_create_and_update_forward_complete_managed_snapshots(
    monkeypatch, tmp_path, handler, operation
):
    captured = []
    snapshot = job(marker="preserved", __profile_home="/untrusted/source")
    monkeypatch.setattr(bridge, "_enabled", lambda: True)
    monkeypatch.setattr(bridge, "MANAGED_HOMES", {tmp_path})
    monkeypatch.setattr(bridge, "MANAGED_PROFILES", {tmp_path: "ops"})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: captured.append(__import__("json").loads(kwargs["input"]))
        or bridge.subprocess.CompletedProcess(args, 0),
    )

    handler(snapshot)

    revision = captured[0]["job"]["record_revision"]
    assert len(revision) == 64
    int(revision, 16)
    assert captured == [{
        "operation": operation,
        "job": {
            "__profile": "ops",
            "id": "job1",
            "enabled": True,
            "state": "scheduled",
            "schedule": {"kind": "cron", "expr": "0 9 * * *"},
            "schedule_display": "0 9 * * *",
            "record_revision": revision,
        },
    }]
    assert "__profile_home" not in captured[0]["job"]
    assert "marker" not in captured[0]["job"]
    assert "name" not in captured[0]["job"]


def test_bridge_update_invokes_authoritative_ops_relay_with_redacted_description_metadata(
    monkeypatch, tmp_path
):
    captured = {}
    private_marker = "PRIVATE-MARKER-12345"
    snapshot = job(
        id="job1",
        name="Operator supplied title",
        name_is_explicit=True,
        prompt=f"private prompt {private_marker}",
        prompt_path="/home/brian/private/prompt.md",
        origin={"platform": "telegram", "chat_id": "private-chat"},
        deliver="discord",
        skills=["ops-skill"],
        skill="ops-skill",
        model="gpt-5",
        provider="openai",
        script="collect.py",
        no_agent=True,
        enabled_toolsets=["web", "terminal"],
        repeat={"times": 3, "completed": 1},
        base_url=f"https://example.test/{private_marker}",
        last_error=f"failed with {private_marker}",
        __profile_home="/untrusted/source",
    )
    monkeypatch.setattr(bridge, "_enabled", lambda: True)
    monkeypatch.setattr(bridge, "MANAGED_HOMES", {tmp_path})
    monkeypatch.setattr(bridge, "MANAGED_PROFILES", {tmp_path: "ops"})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: captured.update({"args": args, "kwargs": kwargs})
        or bridge.subprocess.CompletedProcess(args, 0),
    )

    bridge.on_update(snapshot)

    args = captured["args"][0]
    payload = __import__("json").loads(captured["kwargs"]["input"])
    assert args[:2] == [bridge.sys.executable, str(bridge.OPS_RECONCILER)]
    assert args[2:] == ["--single-job"]
    assert payload == {
        "operation": "update",
        "job": {
            "id": "job1",
            "name": "Operator supplied title",
            "name_is_explicit": True,
            "enabled": True,
            "state": "scheduled",
            "schedule": {"kind": "cron", "expr": "0 9 * * *"},
            "schedule_display": "0 9 * * *",
            "skills": ["ops-skill"],
            "skill": "ops-skill",
            "model": "gpt-5",
            "provider": "openai",
            "script": "collect.py",
            "no_agent": True,
            "repeat": {"times": 3, "completed": 1},
            "deliver": "discord",
            "enabled_toolsets": ["web", "terminal"],
            "record_revision": payload["job"]["record_revision"],
            "__profile": "ops",
        },
    }
    assert len(payload["job"]["record_revision"]) == 64
    int(payload["job"]["record_revision"], 16)
    serialized = __import__("json").dumps(payload)
    assert "private prompt" not in serialized
    assert "prompt_path" not in serialized
    assert "private-chat" not in serialized
    assert private_marker not in serialized
    assert "__profile_home" not in serialized


@pytest.mark.parametrize(
    "delivery",
    [
        "telegram:-1001234567890:42,discord:channel-123,email:person@example.test",
        ["telegram:-1001234567890:42", "discord:channel-123", "email:person@example.test"],
        ("telegram:-1001234567890:42", "discord:channel-123", "email:person@example.test"),
    ],
)
def test_bridge_summarizes_targeted_and_comma_delivery_without_ids(
    monkeypatch, tmp_path, delivery
):
    captured = {}
    snapshot = job(
        deliver=delivery,
        origin={"platform": "telegram", "chat_id": "-1001234567890", "topic_id": "42"},
    )
    monkeypatch.setattr(bridge, "_enabled", lambda: True)
    monkeypatch.setattr(bridge, "MANAGED_HOMES", {tmp_path})
    monkeypatch.setattr(bridge, "MANAGED_PROFILES", {tmp_path: "ops"})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: captured.update({"payload": json.loads(kwargs["input"])})
        or bridge.subprocess.CompletedProcess(args, 0),
    )

    bridge.on_update(snapshot)

    payload = captured["payload"]
    assert payload["job"]["deliver"] == "telegram,discord,email"
    assert len(payload["job"]["record_revision"]) == 64
    serialized = json.dumps(payload, sort_keys=True)
    assert "-1001234567890" not in serialized
    assert "channel-123" not in serialized
    assert "person@example.test" not in serialized
    assert "topic_id" not in serialized


def test_bridge_normalizes_managed_absolute_script_and_rejects_escapes(
    monkeypatch, tmp_path
):
    managed_script = tmp_path / "scripts" / "reports" / "daily.py"
    managed_script.parent.mkdir(parents=True)
    managed_script.write_text("print('ok')\n")
    outside_script = tmp_path / "outside.py"
    outside_script.write_text("print('outside')\n")
    captured = []
    monkeypatch.setattr(bridge, "_enabled", lambda: True)
    monkeypatch.setattr(bridge, "MANAGED_HOMES", {tmp_path})
    monkeypatch.setattr(bridge, "MANAGED_PROFILES", {tmp_path: "ops"})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: captured.append(json.loads(kwargs["input"]))
        or bridge.subprocess.CompletedProcess(args, 0),
    )

    bridge.on_update(job(script=str(managed_script)))
    bridge.on_update(job(id="job2", script=str(outside_script)))
    bridge.on_update(job(id="job3", script="../outside.py"))

    assert captured[0]["job"]["script"] == "scripts/reports/daily.py"
    assert "script" not in captured[1]["job"]
    assert "script" not in captured[2]["job"]


def test_bridge_record_revision_tracks_only_safe_acceptance_metadata(
    monkeypatch, tmp_path
):
    private_marker = "PRIVATE-MARKER-67890"
    base = job(
        prompt=f"do not serialize {private_marker}",
        prompt_path="/tmp/private-prompt.md",
        origin={"platform": "telegram", "chat_id": "chat-123", "topic_id": "topic-9"},
        credentials={"api_key": private_marker},
        base_url=f"https://example.test/{private_marker}",
        root="/tmp/arbitrary-root",
        last_error=f"failed {private_marker}",
        deliver="telegram:chat-123:topic-9",
        skills=["alpha"],
        skill="alpha",
        model="gpt-5",
        provider="openai",
        enabled_toolsets=["web"],
        script="daily.py",
        no_agent=True,
        repeat={"times": 3, "completed": 1},
    )
    captured = []
    monkeypatch.setattr(bridge, "_enabled", lambda: True)
    monkeypatch.setattr(bridge, "MANAGED_HOMES", {tmp_path})
    monkeypatch.setattr(bridge, "MANAGED_PROFILES", {tmp_path: "ops"})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: captured.append(json.loads(kwargs["input"])["job"])
        or bridge.subprocess.CompletedProcess(args, 0),
    )

    variants = {
        "same": {},
        "delivery_target": {"deliver": "telegram:chat-456:topic-9"},
        "skills": {"skills": ["beta"]},
        "skill": {"skill": "beta"},
        "model": {"model": "gpt-5.1"},
        "provider": {"provider": "anthropic"},
        "enabled_toolsets": {"enabled_toolsets": ["terminal"]},
        "script": {"script": "weekly.py"},
        "no_agent": {"no_agent": False},
        "repeat": {"repeat": {"times": 4, "completed": 1}},
        "prompt": {"prompt": f"changed prompt {private_marker}"},
        "prompt_path": {"prompt_path": "/tmp/other-private-prompt.md"},
        "origin": {"origin": {"platform": "telegram", "chat_id": "chat-999"}},
        "credentials": {"credentials": {"token": private_marker}},
        "base_url": {"base_url": f"https://other.example.test/{private_marker}"},
        "root": {"root": "/tmp/other-root"},
        "error_text": {"last_error": f"other failure {private_marker}"},
    }
    for name, overrides in variants.items():
        bridge.on_update(job(**(base | overrides | {"id": name})))

    revisions = {item["id"]: item["record_revision"] for item in captured}
    assert len(revisions["same"]) == hashlib.sha256().digest_size * 2
    assert int(revisions["same"], 16) >= 0
    for key in (
        "delivery_target",
        "skills",
        "skill",
        "model",
        "provider",
        "enabled_toolsets",
        "script",
        "no_agent",
        "repeat",
    ):
        assert revisions[key] != revisions["same"]
    for key in (
        "prompt",
        "prompt_path",
        "origin",
        "credentials",
        "base_url",
        "root",
        "error_text",
    ):
        assert revisions[key] == revisions["same"]

    serialized = json.dumps(captured, sort_keys=True)
    assert private_marker not in serialized
    assert "chat-123" not in serialized
    assert "topic-9" not in serialized
    assert "private-prompt" not in serialized
    assert "arbitrary-root" not in serialized
    assert "other failure" not in serialized


def test_bridge_ignores_legacy_disabled_calendar_sync_config(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(bridge, "MANAGED_HOMES", {tmp_path})
    monkeypatch.setattr(bridge, "MANAGED_PROFILES", {tmp_path: "ops"})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: captured.append(__import__("json").loads(kwargs["input"]))
        or bridge.subprocess.CompletedProcess(args, 0),
    )

    bridge.on_remove(job())

    revision = captured[0]["job"]["record_revision"]
    assert len(revision) == 64
    int(revision, 16)
    assert captured == [{
        "operation": "remove",
        "job": {
            "__profile": "ops",
            "id": "job1",
            "enabled": True,
            "state": "scheduled",
            "schedule": {"kind": "cron", "expr": "0 9 * * *"},
            "schedule_display": "0 9 * * *",
            "record_revision": revision,
        },
    }]


def test_bridge_complete_forwards_managed_output_and_duration(monkeypatch, tmp_path):
    captured = {}
    output = tmp_path / "cron" / "output" / "job1" / "2026-01-01_00-00-00.md"
    output.parent.mkdir(parents=True)
    output.write_text("## Response\ncomplete")
    monkeypatch.setattr(bridge, "_enabled", lambda: True)
    monkeypatch.setattr(bridge, "OPS_RUNNER", tmp_path / "runner.py")
    monkeypatch.setattr(bridge, "MANAGED_HOMES", {tmp_path})
    monkeypatch.setattr(bridge, "MANAGED_PROFILES", {tmp_path: "ops"})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    bridge.OPS_RUNNER.write_text("# runner")
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: captured.update(kwargs)
        or bridge.subprocess.CompletedProcess(args, 0),
    )

    bridge.on_complete(job(), output_file=str(output), duration_seconds=2.5, success=True)

    payload = __import__("json").loads(captured["input"])
    assert payload["operation"] == "complete"
    assert payload["output_file"] == str(output)
    assert payload["duration_seconds"] == 2.5
    assert payload["success"] is True


def test_bridge_rejects_absolute_job_id_output_escape(monkeypatch, tmp_path):
    output = tmp_path / "escaped.md"
    output.write_text("## Response\nnot managed")
    malicious_job = job(id=str(tmp_path))

    monkeypatch.setattr(bridge, "MANAGED_HOMES", {tmp_path / "managed"})

    assert bridge._safe_output_file(str(output), malicious_job) is None


def test_bridge_rejects_unsafe_job_id_before_ops_relay(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(bridge, "_enabled", lambda: True)
    monkeypatch.setattr(bridge, "MANAGED_HOMES", {tmp_path})
    monkeypatch.setattr(bridge, "MANAGED_PROFILES", {tmp_path: "ops"})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    bridge.on_update(job(id="../escaped"))

    assert calls == []


def test_bridge_logs_failed_runner_status_without_stderr(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(bridge, "_enabled", lambda: True)
    monkeypatch.setattr(bridge, "OPS_RUNNER", tmp_path / "runner.py")
    monkeypatch.setattr(bridge, "MANAGED_HOMES", {tmp_path})
    monkeypatch.setattr(bridge, "MANAGED_PROFILES", {tmp_path: "ops"})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    bridge.OPS_RUNNER.write_text("# runner")
    monkeypatch.setattr(
        bridge.subprocess,
        "run",
        lambda *args, **kwargs: bridge.subprocess.CompletedProcess(
            args, 3, stderr="Authorization: sensitive diagnostic"
        ),
    )

    bridge.on_remove(job())

    assert "exited with status 3" in caplog.text
    assert "sensitive diagnostic" not in caplog.text
