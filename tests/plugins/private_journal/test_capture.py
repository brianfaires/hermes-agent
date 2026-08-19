import json
import logging
import os
import stat
from pathlib import Path

import pytest

from plugins.private_journal import capture


def test_log_capture_preserves_verbatim_text_and_writes_secure_record(tmp_path, monkeypatch):
    home = tmp_path / "home-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    raw = "  first line — dash\nsecond line  "

    response = capture.capture_log(raw, source={"platform": "telegram", "chat_type": "dm"})

    assert response.startswith("Logged ")
    holding = home / "journal" / "holding"
    records = list(holding.glob("*.json"))
    assert len(records) == 1
    assert stat.S_IMODE(holding.stat().st_mode) == 0o700
    assert stat.S_IMODE(records[0].stat().st_mode) == 0o600
    data = json.loads(records[0].read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["text"] == raw
    assert data["id"] in response
    assert data["captured_at"] in response
    assert data["source"] == {"platform": "telegram", "chat_type": "dm"}


@pytest.mark.parametrize("raw", ["", "   ", "\n\t  "])
def test_log_capture_empty_usage_writes_nothing(tmp_path, monkeypatch, raw):
    home = tmp_path / "home-b"
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert capture.capture_log(raw) == "Usage: /log <text>"
    assert not (home / "journal" / "holding").exists()


def test_log_capture_rejects_holding_symlink(tmp_path, monkeypatch):
    home = tmp_path / "home-c"
    target = tmp_path / "elsewhere"
    target.mkdir()
    (home / "journal").mkdir(parents=True)
    os.symlink(target, home / "journal" / "holding")
    monkeypatch.setenv("HERMES_HOME", str(home))

    with pytest.raises(capture.PrivateJournalStoreError):
        capture.capture_log("secret")

    assert list(target.iterdir()) == []


def test_log_capture_profile_isolation(tmp_path, monkeypatch):
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"

    monkeypatch.setenv("HERMES_HOME", str(home_a))
    capture.capture_log("alpha")
    monkeypatch.setenv("HERMES_HOME", str(home_b))
    capture.capture_log("beta")

    record_a = next((home_a / "journal" / "holding").glob("*.json"))
    record_b = next((home_b / "journal" / "holding").glob("*.json"))
    assert json.loads(record_a.read_text(encoding="utf-8"))["text"] == "alpha"
    assert json.loads(record_b.read_text(encoding="utf-8"))["text"] == "beta"


def test_log_capture_does_not_log_raw_content(tmp_path, monkeypatch, caplog):
    home = tmp_path / "home-d"
    monkeypatch.setenv("HERMES_HOME", str(home))
    secret = "private content must not appear in logs"

    with caplog.at_level(logging.INFO, logger="plugins.private_journal"):
        capture.capture_log(secret)

    assert secret not in caplog.text
    assert "captured private journal record" in caplog.text


def test_publish_record_id_collision_fails_without_overwrite(tmp_path, monkeypatch):
    home = tmp_path / "home-e"
    monkeypatch.setenv("HERMES_HOME", str(home))
    record = capture.capture_record("first")
    path = capture.publish_record(record)
    original = path.read_text(encoding="utf-8")

    with pytest.raises(FileExistsError):
        capture.publish_record({**record, "text": "second"})

    assert path.read_text(encoding="utf-8") == original
