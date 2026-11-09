"""Refresh-endpoint token_expired retires only the rejected Codex grant."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import time
from urllib.parse import parse_qs

import httpx
import pytest

from agent.credential_pool import STATUS_DEAD, STATUS_EXHAUSTED, load_pool
from hermes_cli import auth as auth_mod


def _synthetic_access(subject: str) -> str:
    claims = {"sub": subject, "exp": int(time.time()) + 28800}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "synthetic." + payload + ".signature"


@pytest.mark.parametrize("status, expected", [(401, STATUS_DEAD), (400, STATUS_DEAD), (429, STATUS_EXHAUSTED)])
@pytest.mark.parametrize("nested", [True, False], ids=["provider-error", "oauth-error"])
def test_refresh_expiry_quarantines_only_rejected_grant(tmp_path, monkeypatch, status, expected, nested):
    home = tmp_path / "authority"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    good_access = _synthetic_access("healthy-user")
    rows = [{"id": name, "label": name, "auth_type": "oauth", "source": "manual:device_code",
             "priority": priority, "access_token": access, "refresh_token": refresh}
            for priority, (name, access, refresh) in enumerate([
                ("healthy", good_access, "synthetic-healthy-refresh"),
                ("rejected", _synthetic_access("rejected-user"), "synthetic-rejected-refresh")])]
    auth_mod._save_auth_store({"version": 1, "credential_pool": {"openai-codex": rows}})
    posts = []

    def token_endpoint(request):
        assert request.method == "POST"
        body = parse_qs(request.content.decode())
        assert body["grant_type"] == ["refresh_token"]
        posts.append(body["refresh_token"][0])
        error = {"error": {"code": "token_expired"}} if nested else {"error": "token_expired"}
        return httpx.Response(status, json=error)

    monkeypatch.setattr("hermes_cli.auth_codex._codex_http_client", lambda **kwargs:
                        httpx.Client(transport=httpx.MockTransport(token_endpoint)))
    pool = load_pool("openai-codex")
    assert pool.try_refresh_matching(credential_id="rejected") is None
    saved = {row["id"]: row for row in auth_mod._load_auth_store()["credential_pool"]["openai-codex"]}
    assert saved["rejected"]["last_status"] == expected
    if expected == STATUS_DEAD:
        assert not saved["rejected"].get("access_token") and not saved["rejected"].get("refresh_token")
    else:
        assert saved["rejected"]["refresh_token"] == "synthetic-rejected-refresh"
    assert (saved["healthy"]["access_token"], saved["healthy"]["refresh_token"]) == (
        good_access, "synthetic-healthy-refresh")
    assert pool.select().id == "healthy"
    assert load_pool("openai-codex").select().id == "healthy"
    assert posts == ["synthetic-rejected-refresh"]
