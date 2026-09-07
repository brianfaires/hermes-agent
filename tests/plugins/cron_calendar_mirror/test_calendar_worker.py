"""Executable-adapter tests: the real worker, in a real subprocess.

Nothing is mocked in-process here. Each test writes a temporary HERMES_HOME
containing a *fake* ``google-workspace`` skill whose ``build_service`` returns
an in-memory stub, then drives the shipped ``calendar_worker.py`` through the
shipped ``CalendarClient``. That covers the parts a fake client cannot: the
JSON wire contract, HTTP-status classification, HERMES_HOME propagation across
the process boundary, and the worker's own write policy.

No network access and no Google client libraries are involved.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from plugins.cron_calendar_mirror.calendar_client import (
    CalendarClient,
    CalendarConflict,
    CalendarError,
    CalendarNotFound,
    CalendarUnavailable,
    missing_prerequisites,
)

CALENDAR_ID = "hermes-crons@group.calendar.google.com"

FAKE_SKILL = '''
"""Stand-in for the profile's google-workspace skill (no Google libraries)."""

import json
import os
import time
from pathlib import Path

STORE = Path(os.environ["HERMES_HOME"]) / "fake_calendar_store.json"
TOKEN_PATH = Path(os.environ["HERMES_HOME"]) / "google_token.json"


def _load():
    return json.loads(STORE.read_text())


def _save(state):
    STORE.write_text(json.dumps(state))


class FakeHttpError(Exception):
    def __init__(self, status, message):
        self.resp = type("Resp", (), {"status": status})()
        super().__init__(message)


class _Call:
    def __init__(self, fn):
        self._fn = fn
        self.headers = {}

    def execute(self):
        return self._fn()


class _Events:
    def insert(self, calendarId, body, **kwargs):
        def run():
            state = _load()
            state.setdefault("insert_kwargs", []).append(sorted(kwargs))
            if state.get("fail_insert"):
                raise FakeHttpError(state["fail_insert"], "injected insert failure")
            if body["id"] in state["events"]:
                raise FakeHttpError(409, "duplicate id")
            state["events"][body["id"]] = dict(body, calendarId=calendarId, etag="fake-etag")
            _save(state)
            return state["events"][body["id"]]
        return _Call(run)

    def patch(self, calendarId, eventId, body, **kwargs):
        def run():
            state = _load()
            if eventId not in state["events"]:
                raise FakeHttpError(404, "no such event")
            if call.headers.get("If-Match") != state["events"][eventId].get("etag") or state.get("race_patch"):
                raise FakeHttpError(412, "event changed concurrently")
            state["events"][eventId].update(body)
            _save(state)
            return state["events"][eventId]
        call = _Call(run)
        return call

    def get(self, calendarId, eventId, **kwargs):
        def run():
            state = _load()
            if state.get("fail_get"):
                raise FakeHttpError(state["fail_get"], "injected get failure")
            if eventId not in state["events"]:
                raise FakeHttpError(404, "no such event")
            return state["events"][eventId]
        return _Call(run)

    def list(self, calendarId, pageToken=None, privateExtendedProperty=None, **kwargs):
        def run():
            state = _load()
            state["last_list"] = {
                "privateExtendedProperty": list(privateExtendedProperty or []),
                "singleEvents": kwargs.get("singleEvents"),
                "showDeleted": kwargs.get("showDeleted"),
                "timeMin": kwargs.get("timeMin"),
                "timeMax": kwargs.get("timeMax"),
            }
            _save(state)
            wanted = set(privateExtendedProperty or [])
            items = []
            for event in state["events"].values():
                private = event.get("extendedProperties", {}).get("private", {})
                tags = {f"{k}={v}" for k, v in private.items()}
                if wanted and not wanted.issubset(tags):
                    continue
                items.append(event)
            # Two pages, so the worker's pagination loop is actually exercised.
            if pageToken is None and len(items) > 1:
                return {"items": items[:1], "nextPageToken": "page2"}
            if pageToken == "page2":
                return {"items": items[1:]}
            return {"items": items}
        return _Call(run)


class _CalendarList:
    def get(self, calendarId, **kwargs):
        def run():
            state = _load()
            if state.get("no_calendar_list"):
                raise FakeHttpError(404, "not in calendarList")
            return {
                "id": calendarId,
                "summary": "Hermes crons",
                "accessRole": state.get("access_role", "owner"),
                "primary": state.get("primary", False),
            }
        return _Call(run)


class _ACL:
    def list(self, **kwargs):
        def run():
            state = _load()
            if state.get("fail_acl"):
                raise FakeHttpError(state["fail_acl"], "ACL unavailable")
            return {"items": state.get("acl", [{"role": "owner", "scope": {"type": "user", "value": "owner@example.test"}}])}
        return _Call(run)


class _Calendars:
    """The calendars collection has no accessRole/primary fields."""

    def get(self, calendarId, **kwargs):
        def run():
            return {"id": calendarId, "summary": "Hermes crons"}
        return _Call(run)


class _Service:
    def events(self):
        return _Events()

    def acl(self):
        return _ACL()

    def calendarList(self):
        return _CalendarList()

    def calendars(self):
        return _Calendars()


def build_service(api, version):
    state = _load()
    state["seen_hermes_home"] = os.environ.get("HERMES_HOME")
    state["seen_api"] = [api, version]
    _save(state)
    if state.get("slow"):
        time.sleep(30)
    return _Service()
'''

FAKE_SKILL_POLICY = FAKE_SKILL + '''

def _resolve_calendar_id(value, service=None):
    state = _load()
    return state.get("alias", {}).get(value, value)


def _require_calendar_action_allowed(command, detail=""):
    state = _load()
    if command in state.get("blocked_commands", []):
        raise SystemExit(f"calendar {command} is blocked by policy")


def _require_calendar_write_allowed(calendar_value, resolved_id=None):
    state = _load()
    if resolved_id in state.get("protected_calendars", []):
        raise SystemExit(f"calendar {resolved_id} is write-protected")
'''


def _write_profile(home: Path, *, skill_source: str = FAKE_SKILL, **state) -> Path:
    scripts = home / "skills" / "productivity" / "google-workspace" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "google_api.py").write_text(skill_source)
    workspace = home / "google-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (home / "google_token.json").write_text(json.dumps({
        "type": "authorized_user",
        "scopes": ["https://www.googleapis.com/auth/calendar.app.created"],
    }))
    store = home / "fake_calendar_store.json"
    store.write_text(json.dumps({"events": {}, **state}))
    return store


def _state(store: Path) -> dict:
    return json.loads(store.read_text())


def _patch_state(store: Path, **values) -> None:
    state = _state(store)
    state.update(values)
    store.write_text(json.dumps(state))


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    home = tmp_path / "profile-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("PYTHONPATH", str(repo_root))
    return home


@pytest.fixture
def client(profile_home):
    return CalendarClient(CALENDAR_ID, approved_owner="owner@example.test", timeout=60)


def _event_body(event_id: str = "hcm0123456789abcdef") -> dict:
    return {
        "id": event_id,
        "summary": "⏱ Nightly digest",
        "description": "Cron job: Nightly digest",
        "start": {"dateTime": "2026-09-07T09:00:00+00:00"},
        "end": {"dateTime": "2026-09-07T09:15:00+00:00"},
        "transparency": "transparent",
        "extendedProperties": {"private": {"hermesMirrorProfile": "ops"}},
    }


# ---------------------------------------------------------------------------
# Round trip through the real subprocess
# ---------------------------------------------------------------------------

def test_full_round_trip_through_the_real_worker(profile_home, client):
    store = _write_profile(profile_home)

    info = client.verify()
    assert info["id"] == CALENDAR_ID
    assert info["aclVerified"] is True

    created = client.insert_event(_event_body())
    assert created["id"] == "hcm0123456789abcdef"

    fetched = client.get_event("hcm0123456789abcdef")
    assert fetched["description"] == "Cron job: Nightly digest"

    client.patch_event("hcm0123456789abcdef", dict(_event_body(), description="updated"))
    assert client.get_event("hcm0123456789abcdef")["description"] == "updated"

    listed = client.list_events(
        time_min="2026-09-01T00:00:00+00:00",
        time_max="2026-09-30T00:00:00+00:00",
        private_property=["hermesMirrorProfile=ops"],
    )
    assert [event["id"] for event in listed] == ["hcm0123456789abcdef"]
    assert _state(store)["last_list"]["privateExtendedProperty"] == [
        "hermesMirrorProfile=ops"
    ]
    assert _state(store)["last_list"]["singleEvents"] is False
    assert _state(store)["last_list"]["showDeleted"] is False



def test_list_follows_pagination(profile_home, client):
    _write_profile(profile_home)
    for index in range(3):
        client.insert_event(_event_body(f"hcmaaaaaaaaaaaaaaaa{index}"))

    listed = client.list_events(
        time_min="2026-09-01T00:00:00+00:00",
        time_max="2026-09-30T00:00:00+00:00",
        private_property=["hermesMirrorProfile=ops"],
    )

    assert len(listed) == 3


def test_worker_runs_against_the_caller_profile_home(profile_home, client, tmp_path):
    """A context-local profile home must cross the subprocess boundary."""
    store = _write_profile(profile_home)
    other = tmp_path / "other-profile"
    other.mkdir()
    _write_profile(other)

    client.verify()

    assert _state(store)["seen_hermes_home"] == str(profile_home)
    assert _state(store)["seen_api"] == ["calendar", "v3"]
    assert "seen_hermes_home" not in _state(other / "fake_calendar_store.json")


def test_verify_falls_back_when_there_is_no_calendar_list_entry(profile_home, client):
    _write_profile(profile_home, no_calendar_list=True)

    with pytest.raises(CalendarError):
        client.verify()


# ---------------------------------------------------------------------------
# Absence vs failure must not collapse
# ---------------------------------------------------------------------------

def test_404_is_confirmed_absence(profile_home, client):
    _write_profile(profile_home)

    with pytest.raises(CalendarNotFound):
        client.get_event("hcmmissing0000000000")


def test_409_is_a_conflict_not_an_absence(profile_home, client):
    _write_profile(profile_home)
    client.insert_event(_event_body())

    with pytest.raises(CalendarConflict):
        client.insert_event(_event_body())


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_non_404_statuses_are_generic_failures(profile_home, client, status):
    _write_profile(profile_home, fail_get=status)

    with pytest.raises(CalendarError) as excinfo:
        client.get_event("hcm0123456789abcdef")

    assert not isinstance(excinfo.value, CalendarNotFound)
    assert not isinstance(excinfo.value, CalendarConflict)


def test_a_malformed_response_is_a_failure_not_an_absence(profile_home, client, monkeypatch):
    _write_profile(profile_home)
    monkeypatch.setattr("plugins.cron_calendar_mirror.calendar_client.worker_python", lambda: "/bin/echo")

    with pytest.raises(CalendarError) as excinfo:
        client.get_event("hcm0123456789abcdef")

    assert not isinstance(excinfo.value, CalendarNotFound)
    assert "malformed" in str(excinfo.value)


def test_a_timeout_is_a_failure_not_an_absence(profile_home, tmp_path, monkeypatch):
    _write_profile(profile_home)
    stub = tmp_path / "slow-python"
    stub.write_text("#!/bin/sh\nsleep 30\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr("plugins.cron_calendar_mirror.calendar_client.worker_python", lambda: str(stub))

    with pytest.raises(CalendarError) as excinfo:
        CalendarClient(CALENDAR_ID, approved_owner="owner@example.test", timeout=1).get_event("hcm0123456789abcdef")

    assert not isinstance(excinfo.value, CalendarNotFound)
    assert "could not run" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Write policy enforced at the boundary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "field, value",
    [
        ("attendees", [{"email": "someone@example.com"}]),
        ("conferenceData", {"createRequest": {}}),
        ("source", {"url": "https://example.com"}),
        ("organizer", {"email": "someone@example.com"}),
    ],
)
def test_audience_widening_fields_are_refused(profile_home, client, field, value):
    store = _write_profile(profile_home)
    body = _event_body()
    body[field] = value

    with pytest.raises(CalendarError) as excinfo:
        client.insert_event(body)

    assert "forbidden fields" in str(excinfo.value)
    assert _state(store)["events"] == {}


def test_written_events_are_forced_private_silent_and_guest_less(profile_home, client):
    store = _write_profile(profile_home)
    body = _event_body()
    body["visibility"] = "public"
    body["guestsCanInviteOthers"] = True
    body["reminders"] = {"useDefault": True}

    client.insert_event(body)

    stored = _state(store)["events"]["hcm0123456789abcdef"]
    assert stored["visibility"] == "private"
    assert stored["guestsCanInviteOthers"] is False
    assert stored["guestsCanModify"] is False
    assert stored["guestsCanSeeOtherGuests"] is False
    assert stored["reminders"] == {"useDefault": False, "overrides": []}
    assert "sendUpdates" in _state(store)["insert_kwargs"][0]


@pytest.mark.parametrize("target", ["primary", "PRIMARY", "", "not-a-calendar-id"])
def test_the_worker_refuses_unsafe_targets(profile_home, target):
    store = _write_profile(profile_home)

    with pytest.raises(CalendarError) as excinfo:
        CalendarClient(target).insert_event(_event_body())

    assert "refusing" in str(excinfo.value) or "empty" in str(excinfo.value)
    assert _state(store)["events"] == {}


def test_an_alias_that_resolves_to_primary_is_still_refused(profile_home):
    """The guard runs on the *resolved* id, not on the configured string."""
    store = _write_profile(
        profile_home,
        skill_source=FAKE_SKILL_POLICY,
        alias={"Hermes crons": "primary"},
    )

    with pytest.raises(CalendarError) as excinfo:
        CalendarClient("Hermes crons").insert_event(_event_body())

    assert "primary" in str(excinfo.value)
    assert _state(store)["events"] == {}


# ---------------------------------------------------------------------------
# The skill's own policy layer, when the profile has one
# ---------------------------------------------------------------------------

def test_the_skills_blocked_command_policy_is_honoured(profile_home):
    store = _write_profile(
        profile_home,
        skill_source=FAKE_SKILL_POLICY,
        blocked_commands=["create"],
    )
    client = CalendarClient(CALENDAR_ID, approved_owner="owner@example.test")

    with pytest.raises(CalendarError) as excinfo:
        client.insert_event(_event_body())

    assert "SystemExit" in str(excinfo.value)
    assert _state(store)["events"] == {}
    # Reads stay available so a blocked profile still reports coherently.
    assert client.verify()["id"] == CALENDAR_ID


def test_the_skills_write_protected_calendar_policy_is_honoured(profile_home):
    store = _write_profile(
        profile_home,
        skill_source=FAKE_SKILL_POLICY,
        protected_calendars=[CALENDAR_ID],
    )

    with pytest.raises(CalendarError) as excinfo:
        CalendarClient(CALENDAR_ID, approved_owner="owner@example.test").insert_event(_event_body())

    assert "SystemExit" in str(excinfo.value)
    assert _state(store)["events"] == {}


def test_a_skill_without_a_policy_layer_still_works(profile_home, client):
    """The repo copy of the skill has no policy helpers; the worker copes."""
    _write_profile(profile_home, skill_source=FAKE_SKILL)

    assert client.insert_event(_event_body())["id"] == "hcm0123456789abcdef"


# ---------------------------------------------------------------------------
# Missing prerequisites fail safe, without spawning anything
# ---------------------------------------------------------------------------

def test_missing_token_reports_unavailable_without_running_the_worker(profile_home):
    _write_profile(profile_home)
    (profile_home / "google_token.json").unlink()

    assert any("token" in reason for reason in missing_prerequisites())
    with pytest.raises(CalendarUnavailable):
        CalendarClient(CALENDAR_ID, approved_owner="owner@example.test").verify()


def test_missing_skill_reports_unavailable(profile_home):
    _write_profile(profile_home)
    (profile_home / "skills" / "productivity" / "google-workspace"
     / "scripts" / "google_api.py").unlink()

    assert any("skill script" in reason for reason in missing_prerequisites())
    with pytest.raises(CalendarUnavailable):
        CalendarClient(CALENDAR_ID, approved_owner="owner@example.test").verify()


def test_an_empty_profile_reports_every_missing_prerequisite(profile_home):
    reasons = missing_prerequisites()

    assert len(reasons) == 2
    assert os.environ["HERMES_HOME"] == str(profile_home)


# ---------------------------------------------------------------------------
# End to end: real cron store -> real worker subprocess -> stored events
# ---------------------------------------------------------------------------

def test_end_to_end_sync_against_a_real_cron_store(profile_home, capsys, monkeypatch):
    """The shipped CLI path, with only the Google SDK replaced."""
    import yaml

    from cron.executions import create_execution, finish_execution
    from cron.jobs import create_job, use_cron_store
    from plugins.cron_calendar_mirror import mirror
    from plugins.cron_calendar_mirror.mirror import PROP_EXECUTION, PROP_KIND

    store = _write_profile(profile_home)
    (profile_home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"entries": {"cron_calendar_mirror": {"settings": {
            "calendar_id": CALENDAR_ID,
            "approved_owner": "owner@example.test",
            "horizon_days": 2,
        }}}},
    }))
    from cron import executions as executions_module

    monkeypatch.setattr(
        executions_module, "EXECUTIONS_FILE", profile_home / "cron" / "executions.db"
    )

    with use_cron_store(profile_home):
        job = create_job(prompt="digest", schedule="every day at 9am", name="Daily digest")
        execution = create_execution(job["id"], source="scheduler")
        finish_execution(execution["id"], success=False, error="exit code 2")

        assert mirror.main(["sync"]) == 0
        first = capsys.readouterr().out
        assert "created" in first

        events = _state(store)["events"]
        kinds = [event["extendedProperties"]["private"][PROP_KIND]
                 for event in events.values()]
        assert kinds.count("run") == 1
        assert kinds.count("schedule") >= 1
        run_event = next(event for event in events.values()
                         if event["extendedProperties"]["private"][PROP_KIND] == "run")
        assert run_event["extendedProperties"]["private"][PROP_EXECUTION] == execution["id"]
        assert "exit code 2" in run_event["description"]
        assert run_event["visibility"] == "private"

        # Second run is a no-op: same state in, same calendar out.
        before = dict(_state(store)["events"])
        assert mirror.main(["sync"]) == 0
        assert "unchanged      : %d" % len(before) in capsys.readouterr().out
        assert _state(store)["events"] == before


@pytest.mark.parametrize("state", [
    {"acl": [{"role": "owner", "scope": {"type": "user", "value": "other@example.test"}}]},
    {"acl": [{"role": "reader", "scope": {"type": "default"}}]},
    {"acl": []}, {"fail_acl": 403}, {"fail_acl": 500},
])
def test_acl_drift_denies_every_write(profile_home, client, state):
    store = _write_profile(profile_home)
    client.insert_event(_event_body())
    before = _state(store)["events"]
    data = _state(store)
    data.update(state)
    store.write_text(json.dumps(data))
    with pytest.raises(CalendarError):
        client.patch_event(_event_body()["id"], dict(_event_body(), description="private result"))
    with pytest.raises(CalendarError):
        client.insert_event(_event_body("hcmanother00000000"))
    assert _state(store)["events"] == before


@pytest.mark.parametrize("change", [
    {"attendees": [{"email": "guest@example.test"}]},
    {"attendeesOmitted": True}, {"visibility": "public"},
    {"visibility": "default"}, {"status": "cancelled"},
    {"extendedProperties": {"private": {"hermesMirrorProfile": "elsewhere"}}},
])
def test_existing_event_cannot_receive_results_with_broader_visibility(profile_home, client, change):
    store = _write_profile(profile_home)
    client.insert_event(_event_body())
    data = _state(store)
    data["events"][_event_body()["id"]].update(change)
    store.write_text(json.dumps(data))
    before = _state(store)["events"]
    with pytest.raises(CalendarError):
        client.patch_event(_event_body()["id"], dict(_event_body(), description="private result"))
    assert _state(store)["events"] == before


def test_missing_explicit_acl_approval_denies_write(profile_home):
    store = _write_profile(profile_home)
    with pytest.raises(CalendarError):
        CalendarClient(CALENDAR_ID).insert_event(_event_body())
    assert _state(store)["events"] == {}


def test_real_scheduler_result_survives_removed_one_shot_and_sync_outage(profile_home, monkeypatch):
    import yaml
    from cron import scheduler, executions
    from cron.jobs import create_job, remove_job, use_cron_store
    from plugins.cron_calendar_mirror import mirror
    from agent.monitoring.cron_health import project_execution_event
    from dataclasses import asdict

    store = _write_profile(profile_home)
    (profile_home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"entries": {"cron_calendar_mirror": {"settings": {
            "calendar_id": CALENDAR_ID, "approved_owner": "owner@example.test",
            "result_page_size": 1,
        }}}},
    }))
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", profile_home / "cron" / "executions.db")
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 1)
    expected = {}

    def fake_run_job(job, *, execution_id, **kwargs):
        result = "Finished answer for " + job["name"]
        expected[execution_id] = (result, job["name"], job["next_run_at"])
        # REMOVE before COMPLETE is valid for an externally removed one-shot.
        remove_job(job["id"])
        return True, "FULL TRANSCRIPT AND TOOL LOG MUST NOT BE RETAINED", result, None

    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    with use_cron_store(profile_home):
        for n in range(4):
            job = create_job(prompt="PRIVATE PROMPT NOT A RESULT", schedule="in 1m", name=f"Fixture {n}", deliver="local")
            assert scheduler.run_one_job(job)
        records = list(executions.iter_execution_results(page_size=1))
        assert len(records) == len(expected) == 4
        assert len(executions.list_executions()) == 1
        for row in records:
            result, name, scheduled = expected[row["id"]]
            assert (row["final_response"], row["job_name"], row["scheduled_at"]) == (result, name, scheduled)
            assert "PRIVATE PROMPT" not in json.dumps(row)
            assert "FULL TRANSCRIPT" not in json.dumps(row)
            event = json.dumps(asdict(project_execution_event(row)))
            assert result not in event and name not in event
            assert executions.finish_execution(row["id"], success=True, final_response="wrong run") is None
        assert mirror.main(["sync"]) == 0
        events = _state(store)["events"]
        assert len(events) == len(expected)
        for event in events.values():
            eid = event["extendedProperties"]["private"][mirror.PROP_EXECUTION]
            assert expected[eid][0] in event["description"]
            assert expected[eid][1] in event["summary"]
        assert mirror.main(["sync"]) == 0
        assert _state(store)["events"] == events


def test_concurrent_guest_change_rejects_result_patch(profile_home, client):
    store = _write_profile(profile_home)
    client.insert_event(_event_body())
    data = _state(store)
    data["race_patch"] = True
    store.write_text(json.dumps(data))
    with pytest.raises(CalendarError):
        client.patch_event(_event_body()["id"], dict(_event_body(), description="new result"))
    assert _state(store)["events"][_event_body()["id"]]["description"] == _event_body()["description"]


@pytest.mark.parametrize("scopes", [None, [], "calendar"])
def test_missing_scope_metadata_never_falls_back_to_broad_scopes(profile_home, client, scopes):
    store = _write_profile(profile_home)
    (profile_home / "google_token.json").write_text(json.dumps({"scopes": scopes}))
    with pytest.raises(CalendarError):
        client.insert_event(_event_body())
    assert _state(store)["events"] == {}
    assert "seen_api" not in _state(store)


def test_malformed_worker_error_does_not_echo_result_content(profile_home, client, monkeypatch, caplog):
    from types import SimpleNamespace
    _write_profile(profile_home)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="private result text", stderr="private result text"))
    with pytest.raises(CalendarError) as caught:
        client.insert_event(_event_body())
    assert "private result text" not in str(caught.value) + caplog.text


def test_cancelled_scheduler_run_does_not_retain_discarded_response(profile_home, monkeypatch):
    from threading import Event
    from cron import scheduler, executions
    from cron.jobs import create_job, use_cron_store

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", profile_home / "cron" / "executions.db")
    cancelled = Event()

    def fake_run_job(job, **kwargs):
        cancelled.set()
        return True, "transcript", "discarded private answer", None

    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    with use_cron_store(profile_home):
        job = create_job(prompt="fixture", schedule="every 1h", deliver="local")
        assert scheduler.run_one_job(job, cancel_event=cancelled)
    retained = list(executions.iter_execution_results())
    assert len(retained) == 1
    assert retained[0]["status"] == "failed"
    assert retained[0].get("final_response") is None
    assert "discarded private answer" not in json.dumps(retained)
