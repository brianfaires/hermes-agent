from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from gateway.kanban_mirror.discord_client import DiscordClient


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
