from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.discord.adapter import (
    DiscordAdapter,
    _discord_outbound_scope_allowed,
    _discord_policy_sets,
    _standalone_send,
)


class FakeChannel:
    def __init__(self, channel_id, parent_id=None):
        self.id = channel_id
        self.parent_id = parent_id


def _adapter(extra):
    adapter = object.__new__(DiscordAdapter)
    adapter.config = SimpleNamespace(extra=extra)
    return adapter


def test_outbound_allows_thread_when_parent_channel_allowed():
    adapter = _adapter({"allowed_channels": "parent-1"})

    allowed, reason = adapter._discord_outbound_channel_allowed(
        FakeChannel("thread-1", parent_id="parent-1")
    )

    assert allowed is True
    assert reason == "allowed"


def test_outbound_allows_explicit_allowed_thread_id():
    adapter = _adapter({"allowed_channels": "thread-1"})

    allowed, reason = adapter._discord_outbound_channel_allowed(
        FakeChannel("thread-1", parent_id="parent-1")
    )

    assert allowed is True
    assert reason == "allowed"


def test_outbound_blocks_thread_when_parent_and_thread_denied():
    adapter = _adapter({"allowed_channels": "default-home"})

    allowed, reason = adapter._discord_outbound_channel_allowed(
        FakeChannel("ops-thread", parent_id="ops-parent")
    )

    assert allowed is False
    assert reason == "channel not in DISCORD_ALLOWED_CHANNELS"


def test_outbound_ignored_channel_overrides_allowed_parent():
    adapter = _adapter({
        "allowed_channels": "ops-parent",
        "ignored_channels": "ops-thread",
    })

    allowed, reason = adapter._discord_outbound_channel_allowed(
        FakeChannel("ops-thread", parent_id="ops-parent")
    )

    assert allowed is False
    assert reason == "channel in DISCORD_IGNORED_CHANNELS"


def test_outbound_empty_allowlist_allows_channel():
    adapter = _adapter({"allowed_channels": "", "ignored_channels": ""})

    allowed, reason = adapter._discord_outbound_channel_allowed(FakeChannel("anywhere"))

    assert allowed is True
    assert reason == "allowed"


def test_policy_sets_parse_list_and_csv_values(monkeypatch):
    monkeypatch.delenv("DISCORD_ALLOWED_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_IGNORED_CHANNELS", raising=False)

    allowed, ignored = _discord_policy_sets({
        "allowed_channels": ["parent-1", 123],
        "ignored_channels": "thread-1, thread-2",
    })

    assert allowed == {"parent-1", "123"}
    assert ignored == {"thread-1", "thread-2"}


def test_policy_sets_fall_back_to_profile_env_when_extra_missing_keys(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "default-home")
    monkeypatch.setenv("DISCORD_IGNORED_CHANNELS", "ops-thread")

    allowed, ignored = _discord_policy_sets({})

    assert allowed == {"default-home"}
    assert ignored == {"ops-thread"}


def test_standalone_policy_helper_preserves_authorized_parent_thread_scope():
    allowed, reason = _discord_outbound_scope_allowed(
        {"thread-1", "parent-1"},
        {"parent-1"},
        set(),
    )

    assert allowed is True
    assert reason == "allowed"


def test_standalone_policy_helper_blocks_unproved_thread_scope():
    allowed, reason = _discord_outbound_scope_allowed(
        {"thread-1"},
        {"parent-1"},
        set(),
    )

    assert allowed is False
    assert reason == "channel not in DISCORD_ALLOWED_CHANNELS"


@pytest.mark.asyncio
async def test_multi_image_send_obeys_outbound_channel_fence():
    adapter = _adapter({"allowed_channels": "default-home"})
    adapter.platform = SimpleNamespace(value="discord")
    channel = SimpleNamespace(id="123", send=AsyncMock())
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )

    await adapter.send_multiple_images(
        "123",
        [("file:///tmp/does-not-matter.png", "caption")],
    )

    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_standalone_thread_send_fails_closed_when_parent_probe_fails(monkeypatch):
    import aiohttp

    posts = []

    class Response:
        def __init__(self, status, payload=None):
            self.status = status
            self.payload = payload or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self):
            return self.payload

        async def text(self):
            return "failure"

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, *_args, **_kwargs):
            return Response(503)

        def post(self, *_args, **_kwargs):
            posts.append(True)
            return Response(200, {"id": "message-1"})

    monkeypatch.setattr(aiohttp, "ClientSession", lambda **_kwargs: Session())
    pconfig = SimpleNamespace(
        token="test-token",
        extra={"allowed_channels": "parent-1"},
    )

    result = await _standalone_send(
        pconfig,
        "parent-1",
        "hello",
        thread_id="thread-1",
    )

    assert result == {"error": "Discord outbound policy could not verify the thread parent"}
    assert posts == []
