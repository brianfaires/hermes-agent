"""Focused boundary coverage for Hindsight cron auto-retention."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.memory import hindsight as hindsight_module
from plugins.memory.hindsight import HindsightMemoryProvider


def _write_config(**overrides) -> Path:
    config = {
        "mode": "cloud",
        "apiKey": "test-key",
        "api_url": "http://hindsight.invalid",
        "bank_id": "test-bank",
        "retain_context": "existing retain context",
    }
    config.update(overrides)
    path = Path(os.environ["HERMES_HOME"]) / "hindsight" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _mock_client():
    client = MagicMock()
    client.aretain_batch = AsyncMock(return_value=SimpleNamespace(operation_ids=[]))
    client.aclose = AsyncMock()
    return client


def _clear_append_capability_cache() -> None:
    with hindsight_module._append_capability_lock:
        hindsight_module._append_capability_cache.clear()


def test_real_config_precedes_environment_and_preserves_false(monkeypatch):
    """A stored false wins, while a missing field still uses its env fallback."""
    _write_config(retain_cron_prompts=False)
    monkeypatch.setenv("HINDSIGHT_RETAIN_CRON_PROMPTS", "true")
    monkeypatch.setenv("HINDSIGHT_RETAIN_CRON_RESULTS", "false")

    provider = HindsightMemoryProvider()
    provider.initialize(session_id="cron-session", platform="cron")

    assert provider._retain_cron_prompts is False
    assert provider._retain_cron_results is False


@pytest.mark.parametrize(
    (
        "retain_prompt",
        "retain_result",
        "api_version",
        "retain_async",
        "expected_role",
        "expected_contents",
    ),
    [
        (
            True,
            False,
            "0.6.1",
            True,
            "user",
            [
                "Cron automation prompt: first scheduler prompt",
                "Cron automation prompt: second scheduler prompt",
            ],
        ),
        (
            False,
            True,
            None,
            False,
            "assistant",
            [
                "Cron run result: first substantive result",
                "Cron run result: second substantive result",
            ],
        ),
    ],
)
def test_one_sided_switch_flush_preserves_payload_contract(
    monkeypatch,
    retain_prompt,
    retain_result,
    api_version,
    retain_async,
    expected_role,
    expected_contents,
):
    """Switch flush keeps context, lineage, counts, update mode, and async mode."""
    _write_config(
        retain_cron_prompts=retain_prompt,
        retain_cron_results=retain_result,
        retain_every_n_turns=3,
        retain_async=retain_async,
    )
    _clear_append_capability_cache()
    monkeypatch.setattr(
        hindsight_module,
        "_fetch_hindsight_api_version",
        lambda *args, **kwargs: api_version,
    )

    provider = HindsightMemoryProvider()
    provider.initialize(
        session_id="old-cron-session",
        parent_session_id="root-session",
        platform="cron",
    )
    provider._client = _mock_client()

    provider.sync_turn("first scheduler prompt", "first substantive result")
    provider.sync_turn("second scheduler prompt", "second substantive result")
    provider.on_session_switch("new-cron-session", parent_session_id="old-cron-session")
    provider._retain_queue.join()

    provider._client.aretain_batch.assert_awaited_once()
    call = provider._client.aretain_batch.await_args.kwargs
    item = call["items"][0]
    turns = json.loads(item["content"])

    assert [[message["role"] for message in turn] for turn in turns] == [
        [expected_role],
        [expected_role],
    ]
    assert [turn[0]["content"] for turn in turns] == expected_contents
    assert item["metadata"] == {
        "retained_at": item["metadata"]["retained_at"],
        "message_count": "2",
        "turn_index": "2",
        "session_id": "old-cron-session",
        "platform": "cron",
    }
    assert item["tags"] == ["session:old-cron-session", "parent:root-session"]
    assert item["context"].startswith("existing retain context\n\n")
    assert "scheduler-authored context, not user-authored" in item["context"]
    assert "Extract only substantive outcomes evidenced by the cron run result" in item["context"]
    assert call["retain_async"] is retain_async
    if api_version is None:
        assert call["document_id"].startswith("old-cron-session-")
        assert "update_mode" not in item
    else:
        assert call["document_id"] == "old-cron-session"
        assert item["update_mode"] == "append"


class _InMemoryTransportResponse:
    status = 200
    reason = "OK"

    def __init__(self, *, retain_async: bool):
        self.data = json.dumps(
            {
                "success": True,
                "bank_id": "test-bank",
                "items_count": 1,
                "async": retain_async,
            }
        ).encode("utf-8")
        self._headers = {"content-type": "application/json"}

    async def read(self):
        return self.data

    def getheaders(self):
        return self._headers

    def getheader(self, name, default=None):
        return self._headers.get(name.lower(), default)


def test_real_sdk_serializes_result_only_payload_at_http_transport(monkeypatch):
    """Exercise provider config through hindsight-client's HTTP transport boundary."""
    hindsight_client = pytest.importorskip("hindsight_client")
    _write_config(
        retain_cron_prompts=False,
        retain_cron_results=True,
        retain_async=False,
    )
    _clear_append_capability_cache()
    monkeypatch.setattr(
        hindsight_module,
        "_fetch_hindsight_api_version",
        lambda *args, **kwargs: "0.6.1",
    )

    provider = HindsightMemoryProvider()
    provider.initialize(session_id="cron-sdk-session", platform="cron")
    client = hindsight_client.Hindsight(
        base_url="http://hindsight.invalid",
        api_key="test-key",
    )
    captured = {}

    async def _request(method, url, *, headers=None, body=None, **kwargs):
        captured.update(method=method, url=url, headers=headers, body=body)
        return _InMemoryTransportResponse(retain_async=body["async"])

    monkeypatch.setattr(client._api_client.rest_client, "request", _request)
    provider._client = client
    try:
        provider.sync_turn("scheduler-only input", "deployment completed")
        provider._retain_queue.join()

        assert captured["method"] == "POST"
        assert captured["url"].endswith("/v1/default/banks/test-bank/memories")
        assert captured["body"]["async"] is False
        item = captured["body"]["items"][0]
        assert item["document_id"] == "cron-sdk-session"
        assert item["update_mode"] == "append"
        assert item["metadata"]["message_count"] == "1"
        assert json.loads(item["content"]) == [[
            {
                "role": "assistant",
                "content": "Cron run result: deployment completed",
                "timestamp": json.loads(item["content"])[0][0]["timestamp"],
            }
        ]]
        assert "scheduler-authored context, not user-authored" in item["context"]
    finally:
        provider.shutdown()
