"""FC-16: exercise adapter/REST egress with real imports and isolated home.

Derived from legacy 634d96a7; only the network boundary is replaced.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter, _standalone_send


def adapter_for(extra, channel):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test", extra=extra))
    adapter._client = SimpleNamespace(get_channel=lambda _: channel, fetch_channel=AsyncMock(return_value=channel))
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["send", "edit_message", "send_image_file", "send_document", "send_video", "send_voice", "send_multiple_images", "send_image", "send_animation"])
async def test_denied_channel_never_delivers(method, tmp_path):
    msg = SimpleNamespace(id=77, attachments=[object()], edit=AsyncMock())
    channel = SimpleNamespace(id=123, name="denied", parent_id=None, send=AsyncMock(return_value=msg), get_partial_message=lambda _: msg)
    adapter = adapter_for({"allowed_channels": ["456"]}, channel)
    media = tmp_path / "image.png"
    media.write_bytes(b"test")
    args = {
        "send": ("123", "secret"), "edit_message": ("123", "77", "secret"),
        "send_image_file": ("123", str(media)), "send_document": ("123", str(media)),
        "send_video": ("123", str(media)), "send_voice": ("123", str(media)),
        "send_multiple_images": ("123", [(media.as_uri(), "secret")]),
        "send_image": ("123", "https://example.org/image.png"),
        "send_animation": ("123", "https://example.org/image.gif"),
    }
    # Denial must precede any download/upload and report the policy failure.
    result = await getattr(adapter, method)(*args[method])
    channel.send.assert_not_awaited()
    msg.edit.assert_not_awaited()
    if method != "send_multiple_images":
        assert not result.success
        assert "DISCORD_ALLOWED_CHANNELS" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed,ignored,success", [("456", "", True), ("123", "456", False), ("*", "*", False), ("", "", True)])
async def test_actual_thread_scope_and_deny_precedence(allowed, ignored, success):
    channel = SimpleNamespace(id=123, name="thread", parent_id=456, send=AsyncMock(return_value=SimpleNamespace(id=77)))
    adapter = adapter_for({"allowed_channels": allowed, "ignored_channels": ignored}, channel)
    result = await adapter.send("999", "hello", metadata={"thread_id": "123"})
    assert result.success is success
    assert channel.send.await_count == int(success)


@pytest.mark.asyncio
async def test_connected_profile_snapshot_is_authoritative(monkeypatch):
    channel = SimpleNamespace(id=123, name="private", send=AsyncMock())
    adapter = adapter_for({"allowed_channels": "123"}, channel)
    adapter._gate_env_snapshot = {"DISCORD_ALLOWED_CHANNELS": "456", "DISCORD_IGNORED_CHANNELS": ""}
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "123")
    result = await adapter.send("123", "secret")
    assert not result.success
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id,info,status,allowed,ignored,success", [
    ("123", {}, 503, "456", "", False),
    ("123", {"id":"123", "type":11,"parent_id":"789"}, 200, "456", "", False),
    ("123", {"id":"123", "type":11,"parent_id":"456"}, 200, "456", "", True),
    (None, {"id":"456", "type":11,"parent_id":"789"}, 200, "456", "789", False),
    (None, {"id":"456", "type":0,"name":"home"}, 200, "#home", "", True),
])
async def test_standalone_proves_resolved_scope(monkeypatch, thread_id, info, status, allowed, ignored, success):
    import aiohttp
    posts=[]
    class Response:
        def __init__(self, status, data): self.status,self.data=status,data
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def json(self): return self.data
        async def text(self): return "failure"
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        def get(self,*args,**kwargs):
            requested = args[0].rsplit("/", 1)[-1]
            if requested != str(info.get("id")) and status == 200:
                return Response(200, {"id": requested, "type": 0, "name": "parent"})
            return Response(status,info)
        def post(self,*args,**kwargs):
            posts.append(args[0]); return Response(200,{"id":"77"})
    monkeypatch.setattr(aiohttp,"ClientSession",lambda **kwargs: Session())
    result=await _standalone_send(SimpleNamespace(token="test",extra={"allowed_channels":allowed,"ignored_channels":ignored}),"456","secret",thread_id=thread_id)
    assert bool(result.get("success")) is success
    assert bool(posts) is success


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args", [
    ("send_exec_approval", ("123", "secret command", "caller")),
    ("send_slash_confirm", ("123", "Confirm", "secret", "caller", "id")),
    ("send_clarify", ("123", "secret", [], "id", "caller")),
    ("send_update_prompt", ("123", "secret")),
    ("send_model_picker", ("123", [], "secret", "provider", "caller", lambda _: None)),
    ("send_choice_picker", ("123", "secret", [], "caller", lambda _: None)),
])
async def test_control_payloads_obey_same_fence(method, args):
    channel = SimpleNamespace(id=123, send=AsyncMock())
    adapter = adapter_for({"ignored_channels": "123"}, channel)
    result = await getattr(adapter, method)(*args)
    assert not result.success
    assert "DISCORD_IGNORED_CHANNELS" in result.error
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_denied_forum_cannot_create_post():
    channel = SimpleNamespace(id=123, type=15, create_thread=AsyncMock())
    adapter = adapter_for({"ignored_channels": "123"}, channel)
    result = await adapter.send("123", "secret")
    assert not result.success
    channel.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncached_thread_parent_name_cannot_bypass_deny():
    channel = SimpleNamespace(id=123, name="thread", parent_id=456, parent=None, send=AsyncMock(return_value=SimpleNamespace(id=77)))
    adapter = adapter_for({"allowed_channels":"*", "ignored_channels":"#private"}, channel)
    adapter._client.fetch_channel = AsyncMock(return_value=SimpleNamespace(id=456, name="private"))
    result = await adapter.send("123", "secret")
    assert not result.success
    channel.send.assert_not_awaited()
