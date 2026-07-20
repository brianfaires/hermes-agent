"""Profile-isolation contracts for the Calendar subprocess boundary."""

import json
from types import SimpleNamespace

from hermes_plugins.cron_calendar_sync import calendar_client as client


def test_worker_receives_effective_profile_home(tmp_path, monkeypatch):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "result": {"items": []}}),
            stderr="",
        )

    monkeypatch.setattr(client, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(client.subprocess, "run", fake_run)

    assert client.list_events("Hermes crons") == []
    assert captured["env"]["HERMES_HOME"] == str(tmp_path)
