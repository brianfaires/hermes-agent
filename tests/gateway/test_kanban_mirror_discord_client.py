from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.kanban_mirror.discord_client import DiscordAPIError, DiscordClient


class _DummyResponse:
    def __init__(self, status: int = 200, payload: str = '{"id":"th1","message":{"id":"m1"}}'):
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._payload.encode("utf-8")


def test_create_forum_thread_uses_multipart_for_attachments(tmp_path, monkeypatch):
    attachment = tmp_path / "report.pdf"
    attachment.write_bytes(b"PDFDATA")

    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["data"] = req.data
        return _DummyResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = DiscordClient("token")
    result = client.create_forum_thread(
        "123",
        name="Review card",
        content="Starter post\nMEDIA:/tmp/evidence/report.pdf",
        tag_ids=["456"],
        attachments=[str(attachment)],
    )

    assert result["id"] == "th1"
    assert captured["url"].endswith("/channels/123/threads")
    headers = captured["headers"]
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    assert "multipart/form-data" in content_type
    assert captured["data"] is not None
    body = captured["data"]
    assert b'payload_json' in body
    assert b'filename="report.pdf"' in body
    assert b"PDFDATA" in body
    payload_marker = body.index(b'payload_json')
    assert b'Starter post\\nMEDIA:/tmp/evidence/report.pdf' in body[payload_marker:]


def test_list_active_threads_uses_guild_endpoint_and_filters_payload(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        return _DummyResponse(payload=json.dumps({
            "threads": [
                {"id": "th1", "parent_id": "forum1"},
                "not-a-thread",
                {"id": "th2", "parent_id": "forum2"},
            ],
        }))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    threads = DiscordClient("token").list_active_threads("guild1")

    assert captured["url"].endswith("/guilds/guild1/threads/active")
    assert threads == [
        {"id": "th1", "parent_id": "forum1"},
        {"id": "th2", "parent_id": "forum2"},
    ]


def test_list_active_threads_handles_malformed_threads_payload(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=30: _DummyResponse(payload=json.dumps({"threads": {"id": "not-a-list"}})),
    )

    assert DiscordClient("token").list_active_threads("guild1") == []


def test_get_retries_transient_network_timeouts(monkeypatch):
    attempts = []

    def flaky_urlopen(req, timeout=30):
        attempts.append(req.full_url)
        if len(attempts) < 3:
            raise urllib.error.URLError(TimeoutError("TLS handshake timed out"))
        return _DummyResponse(payload=json.dumps({"threads": []}))

    monkeypatch.setattr("urllib.request.urlopen", flaky_urlopen)
    monkeypatch.setattr("time.sleep", lambda _: None)

    assert DiscordClient("token").list_active_threads("guild1") == []
    assert len(attempts) == 3


def test_get_retries_transient_discord_503(monkeypatch):
    attempts = []

    def flaky_urlopen(req, timeout=30):
        attempts.append(req.full_url)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(
                req.full_url, 503, "unavailable", Message(), io.BytesIO(b"upstream unavailable")
            )
        return _DummyResponse(payload=json.dumps({"threads": []}))

    monkeypatch.setattr("urllib.request.urlopen", flaky_urlopen)
    monkeypatch.setattr("time.sleep", lambda _: None)

    assert DiscordClient("token").list_active_threads("guild1") == []
    assert len(attempts) == 2


@pytest.mark.parametrize("status", [502, 503, 504])
def test_get_stops_after_transient_http_retries(monkeypatch, status):
    attempts = []

    def always_fails(req, timeout=30):
        attempts.append(req.full_url)
        raise urllib.error.HTTPError(
            req.full_url, status, "unavailable", Message(), io.BytesIO(b"upstream unavailable")
        )

    monkeypatch.setattr("urllib.request.urlopen", always_fails)
    monkeypatch.setattr("time.sleep", lambda _: None)

    with pytest.raises(DiscordAPIError) as exc:
        DiscordClient("token").list_active_threads("guild1")

    assert exc.value.status == status
    assert len(attempts) == 3


def test_non_get_request_does_not_retry_transient_http_error(monkeypatch):
    attempts = []

    def always_fails(req, timeout=30):
        attempts.append(req.full_url)
        raise urllib.error.HTTPError(
            req.full_url, 503, "unavailable", Message(), io.BytesIO(b"upstream unavailable")
        )

    monkeypatch.setattr("urllib.request.urlopen", always_fails)
    monkeypatch.setattr("time.sleep", lambda _: None)

    with pytest.raises(DiscordAPIError):
        DiscordClient("token").update_thread("thread1", name="Updated")

    assert len(attempts) == 1
