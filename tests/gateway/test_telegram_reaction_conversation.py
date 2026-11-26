"""Ordinary reaction handoff contracts; Telegram SDK is mocked by gateway conftest."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.mark.asyncio
async def test_registered_reaction_enters_normal_external_handoff(adapter, monkeypatch):
    from plugins.platforms.telegram import adapter as telegram_adapter

    delivered = []
    del adapter.handle_message  # Exercise the ordinary base-adapter admission path.
    adapter._message_handler = AsyncMock()
    monkeypatch.setattr(adapter, "_start_session_processing", lambda event, key: delivered.append(event) or True)
    adapter._owner_profile = "receiving"
    app = Mock()
    with patch("telegram.ext.MessageReactionHandler") as native_handler, patch.object(telegram_adapter, "TypeHandler") as observer:
        adapter._register_handlers(app)
        callback = native_handler.call_args.args[0]
        app.add_handler.assert_any_call(native_handler.return_value)
        app.add_handler.assert_any_call(observer.return_value, group=99)
    await callback(ordinary_update(), None)
    assert len(delivered) == 1
    event = delivered[0]
    assert event.source.user_id == "88"
    assert event.source.chat_id == "-100"
    assert event.source.chat_type == "group"
    assert event.source.profile == "receiving"
    assert event.source._transport_adapter_ref() is adapter
    assert event.message_id == event.source.message_id == "reaction:10"
    assert event.reply_to_message_id == event.reply_anchor_override == "472"
    assert event.platform_update_id == 10
    assert event.internal is False
    assert event.allow_gateway_control is False
    assert event.prompt_response is None
    assert event.raw_message is None
    assert event.source.role_authorized is False
    assert "not consent, approval, or an instruction" in event.text
    assert "Target text and author unavailable" in event.text


def ordinary_update():
    return SimpleNamespace(update_id=10, message_reaction=SimpleNamespace(
        user=SimpleNamespace(id=88, is_bot=False), actor_chat=None,
        chat=SimpleNamespace(id=-100, type="supergroup", is_forum=False),
        message_id=472, date=datetime.now(UTC),
        old_reaction=[], new_reaction=[SimpleNamespace(type="emoji", emoji="👍")]))


@pytest.fixture
def adapter():
    instance = TelegramAdapter(PlatformConfig(enabled=True, token="111:offline-test", extra={
        "group_allow_from": ["88"], "allowed_chats": ["-100"], "require_mention": True,
    }))
    instance.handle_message = AsyncMock()
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
@pytest.mark.parametrize("forum_marker", [None, False])
async def test_optional_nonforum_marker_is_ordinary_group(adapter, chat_type, forum_marker):
    update = ordinary_update()
    update.message_reaction.chat.type = chat_type
    update.message_reaction.chat.is_forum = forum_marker
    await adapter._handle_reaction_message(update, None)
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "group"
    assert event.source.thread_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("old,new", [
    ([], [("emoji", "👍")]),
    ([("emoji", "👍")], []),
    ([("custom_emoji", "000123")], [("paid", None)]),
])
async def test_exact_state_and_routed_profile(adapter, old, new):
    def state(items):
        return [SimpleNamespace(type=kind, **({"emoji" if kind == "emoji" else "custom_emoji_id": value}
                                            if kind != "paid" else {})) for kind, value in items]

    update = ordinary_update()
    update.message_reaction.old_reaction = state(old)
    update.message_reaction.new_reaction = state(new)
    adapter._owner_profile = "receiving"
    adapter.gateway_runner = SimpleNamespace(_profile_name_for_source=lambda source, adapter_profile: "routed")
    await adapter._handle_reaction_message(update, None)
    event = adapter.handle_message.await_args.args[0]
    observation = json.loads(event.text.splitlines()[-1])
    assert observation == {
        "actor_id": 88, "chat_id": -100, "target_message_id": 472, "update_id": 10,
        "date": update.message_reaction.date.isoformat(),
        "old_reaction": [list(item) for item in old], "new_reaction": [list(item) for item in new],
    }
    assert event.source.profile == "routed"
    assert event.source._transport_adapter_ref() is adapter
    assert event.metadata == {}
    assert not event.is_command()
    assert event.get_command() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path,value", [
    ("user", None), ("user.id", True), ("user.id", "88"), ("user.id", 0),
    ("user.is_bot", True), ("user.is_bot", None), ("user.is_bot", 0),
    ("actor_chat", SimpleNamespace(id=-200)),
    ("chat.id", True), ("chat.id", "-100"), ("chat.id", 100),
    ("chat.type", "private"), ("chat.type", "channel"), ("chat.type", None),
    ("chat.is_forum", True), ("chat.is_forum", 0), ("chat.is_forum", "false"),
    ("message_id", True), ("message_id", "472"), ("message_id", 0),
    ("date", None), ("date", datetime(2026, 1, 1)),
    ("old_reaction", None), ("new_reaction", {}),
    ("new_reaction", [SimpleNamespace(type="unknown")]),
    ("new_reaction", [SimpleNamespace(type="emoji", emoji="")]),
    ("new_reaction", [SimpleNamespace(type="emoji", emoji="a" * 65)]),
    ("new_reaction", [SimpleNamespace(type="custom_emoji", custom_emoji_id=True)]),
    ("new_reaction", [SimpleNamespace(type="custom_emoji", custom_emoji_id="1" * 129)]),
    ("old_reaction", [SimpleNamespace(type="paid")] * 65),
    ("new_reaction", [SimpleNamespace(type="paid")] * 65),
])
async def test_malformed_or_unsupported_reaction_does_not_handoff(adapter, path, value):
    update = ordinary_update()
    target = update.message_reaction
    parts = path.split(".")
    for part in parts[:-1]:
        target = getattr(target, part)
    setattr(target, parts[-1], value)
    await adapter._handle_reaction_message(update, None)
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("update_id", [True, -1, "10", None])
async def test_invalid_update_identity_does_not_handoff(adapter, update_id):
    update = ordinary_update()
    update.update_id = update_id
    await adapter._handle_reaction_message(update, None)
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("age,accepted", [(119, True), (121, False), (-4, True), (-6, False)])
async def test_freshness(adapter, age, accepted):
    update = ordinary_update()
    update.message_reaction.date -= timedelta(seconds=age)
    await adapter._handle_reaction_message(update, None)
    assert bool(adapter.handle_message.await_count) is accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"group_allow_from": ["99"]}, {"allowed_chats": ["-200"]}, {"allowed_topics": ["7"]},
])
async def test_existing_actor_chat_and_topic_gates(adapter, extra):
    adapter.config.extra.update(extra)
    await adapter._handle_reaction_message(ordinary_update(), None)
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_unchanged_state_does_not_handoff(adapter):
    update = ordinary_update()
    update.message_reaction.old_reaction = list(update.message_reaction.new_reaction)
    await adapter._handle_reaction_message(update, None)
    adapter.handle_message.assert_not_awaited()
