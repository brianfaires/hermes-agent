"""Telegram's ordinary text preview policy also applies to grouped tool progress."""

import asyncio
from itertools import count
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import run_turn_progress
from gateway.config import Platform, PlatformConfig
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from plugins.platforms.telegram import adapter as telegram


@pytest.fixture(params=[True, False], ids=["modern-sdk", "legacy-sdk"])
def make_adapter(request, monkeypatch):
    modern = request.param
    monkeypatch.setattr(telegram, "LinkPreviewOptions", SimpleNamespace if modern else None)

    def make(disable_previews):
        adapter = telegram.TelegramAdapter(
            PlatformConfig(enabled=True, token="fake-token", extra={"disable_link_previews": disable_previews})
        )
        adapter._bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=456)),
            edit_message_text=AsyncMock(),
            send_chat_action=AsyncMock(),
        )
        return adapter

    return make, modern


def assert_preview_policy(payload, disable_previews, modern):
    if disable_previews and modern:
        assert payload["link_preview_options"].is_disabled is True
    else:
        assert "link_preview_options" not in payload
    if disable_previews and not modern:
        assert payload["disable_web_page_preview"] is True
    else:
        assert "disable_web_page_preview" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("disable_previews", [True, False], ids=["disabled", "default-previews"])
@pytest.mark.parametrize("plain_fallback", [False, True], ids=["markdown", "plain-fallback"])
async def test_grouped_progress_uses_same_preview_policy_on_send_and_edit(
    make_adapter, monkeypatch, disable_previews, plain_fallback,
):
    make, modern = make_adapter
    adapter = make(disable_previews)
    bot = adapter._bot
    expected_edits = 2 if plain_fallback else 1
    if plain_fallback:
        bot.edit_message_text.side_effect = [ValueError("can't parse entities"), None]

    # Advance only the progress loop's throttle clock; keep real send/edit/typing plumbing.
    monkeypatch.setattr(run_turn_progress, "time", SimpleNamespace(monotonic=count(2, 2).__next__))
    first = "Searching https://example.com/first"
    second = "Reading https://example.com/second"
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123")
    ctx = TurnContext(
        source=source,
        progress_queue=Queue(),
        _run_still_current=lambda: bot.edit_message_text.await_count < expected_edits,
    )
    ctx.progress_queue.put(first)
    ctx.progress_queue.put(second)
    runner = TurnRunner(SimpleNamespace(_delivery_adapter_for=lambda _: adapter), ctx)

    await asyncio.wait_for(runner.send_progress_messages(), timeout=5)

    bot.send_message.assert_awaited_once()
    send = bot.send_message.await_args.kwargs
    assert send["chat_id"] == 123
    assert send["text"] == r"Searching https://example\.com/first"
    assert_preview_policy(send, disable_previews, modern)

    edits = [call.kwargs for call in bot.edit_message_text.await_args_list]
    assert len(edits) == expected_edits
    assert edits[0]["parse_mode"] == telegram.ParseMode.MARKDOWN_V2
    assert edits[0]["text"] == r"Searching https://example\.com/first" + "\n" + r"Reading https://example\.com/second"
    if plain_fallback:
        assert "parse_mode" not in edits[1]
        assert edits[1]["text"] == f"{first}\n{second}"
    for edit in edits:
        assert (edit["chat_id"], edit["message_id"]) == (123, 456)
        assert_preview_policy(edit, disable_previews, modern)


@pytest.mark.asyncio
@pytest.mark.parametrize("disable_previews", [True, False], ids=["disabled", "default-previews"])
async def test_ordinary_streaming_reply_edit_uses_preview_policy(make_adapter, disable_previews):
    make, modern = make_adapter
    adapter = make(disable_previews)
    text = "Reply https://example.com/reply"

    result = await adapter.edit_message("123", "456", text, finalize=False)

    assert result.success
    adapter._bot.send_message.assert_not_awaited()
    adapter._bot.edit_message_text.assert_awaited_once()
    payload = adapter._bot.edit_message_text.await_args.kwargs
    assert (payload["chat_id"], payload["message_id"], payload["text"]) == (123, 456, text)
    assert "parse_mode" not in payload
    assert_preview_policy(payload, disable_previews, modern)
