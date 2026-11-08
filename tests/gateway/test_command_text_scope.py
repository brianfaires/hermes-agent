"""Verbatim /command text is carried for text-mode plugin commands only (#130).

Built-ins and option-mode plugin commands keep trimmed args and mention cleaning.
"""
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner, _profile_runtime_scope
from hermes_cli.plugins import get_plugin_manager

NOTE = " café — note\n\n    pasted line\nend \n"


def _install(home):
    plugin = home / "plugins" / "scope-example"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: scope-example\nversion: 1.0.0\n")
    (plugin / "__init__.py").write_text(
        "from hermes_constants import get_hermes_home\n"
        "def capture(raw):\n"
        '    (get_hermes_home() / "captured.txt").write_text(raw)\n'
        '    return "Logged"\n'
        "def register(ctx):\n"
        '    ctx.register_command("log", capture, args_hint="<text>", argument_mode="text")\n'
        '    ctx.register_command("flags", capture, args_hint="<flag>", argument_mode="options")\n')
    (home / "config.yaml").write_text("plugins:\n  enabled: [scope-example]\n")
    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    assert not manager._plugins["scope-example"].error


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with _profile_runtime_scope(tmp_path):
        _install(tmp_path)
        yield tmp_path


def _discord_adapter():
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tests.gateway.test_discord_slash_commands import FakeTree

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._client = SimpleNamespace(tree=FakeTree(), user=SimpleNamespace(id=99999))
    adapter._run_simple_slash = AsyncMock()
    adapter._register_slash_commands()
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("name, args, expected", [
    ("goal", "  ship it \n", "/goal   ship it"),       # built-in
    ("flags", "  --x \n", "/flags   --x"),             # option-mode plugin
    ("log", NOTE, "/log " + NOTE),                   # text-mode plugin
])
async def test_discord_slash_trims_except_text_plugins(home, name, args, expected):
    adapter = _discord_adapter()
    await adapter._client.tree.commands[name].callback(SimpleNamespace(), args=args)
    adapter._run_simple_slash.assert_awaited_once_with(SimpleNamespace(), expected)


async def _discord_event(adapter, content):
    import discord

    bot = SimpleNamespace(id=99999, name="Hermes", bot=True)
    adapter._client = SimpleNamespace(user=bot)
    events = []
    adapter.handle_message = AsyncMock(side_effect=events.append)
    channel = Mock(spec=discord.DMChannel)
    channel.id = 7
    message = SimpleNamespace(
        id=42, content=content, channel=channel, guild=None,
        author=SimpleNamespace(id=2, name="owner", display_name="Owner", bot=False),
        mentions=[bot], message_snapshots=[], attachments=[], reference=None,
        created_at=datetime.now(UTC),
    )
    with _profile_runtime_scope(Path("/nonexistent-transport")):
        assert await adapter._handle_message(message)
    return events[0]


async def _dispatch(event):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    return await runner._hm_dispatch_plugin_command(event, event.source, event.get_command())


@pytest.mark.asyncio
async def test_discord_ingress_builtin_args_stay_clean(home):
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    event = await _discord_event(adapter, "/goal  ship <@99999> it \n")
    assert event.get_command() == "goal"
    assert event.get_command_args() == "ship  it"


@pytest.mark.asyncio
async def test_discord_ingress_option_plugin_args_stay_clean(home):
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    event = await _discord_event(adapter, "/flags  —x <@99999> \n")
    assert (await _dispatch(event))[0]
    assert (home / "captured.txt").read_text() == "--x"


@pytest.mark.asyncio
async def test_discord_ingress_text_plugin_keeps_verbatim_note(home):
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
    command = "/log  café — <@99999>\n\n    pasted line\nend \n"
    event = await _discord_event(adapter, "<@99999> " + command)
    assert (await _dispatch(event))[0]
    assert (home / "captured.txt").read_text() == command[len("/log "):]


async def _telegram_event(adapter, text):
    from tests.gateway.test_telegram_long_command_batching import _make_update

    with _profile_runtime_scope(Path("/nonexistent-transport")):
        await adapter._handle_command(_make_update(text), SimpleNamespace())
    return adapter.handle_message.call_args.args[0]


@pytest.mark.asyncio
async def test_telegram_builtin_and_option_args_drop_own_handle(home):
    from tests.gateway.test_telegram_long_command_batching import _make_adapter

    adapter = _make_adapter()
    builtin = await _telegram_event(adapter, "/goal @test_bot ship it")
    assert builtin.get_command_args() == "ship it"
    option = await _telegram_event(adapter, "/flags@test_bot @test_bot x")
    assert (await _dispatch(option))[0]
    assert (home / "captured.txt").read_text() == "x"


@pytest.mark.asyncio
async def test_telegram_text_plugin_keeps_verbatim_note(home):
    from tests.gateway.test_telegram_long_command_batching import _make_adapter

    adapter = _make_adapter()
    event = await _telegram_event(adapter, "/log@test_bot " + NOTE.replace("note", "@test_bot"))
    assert (await _dispatch(event))[0]
    assert (home / "captured.txt").read_text() == NOTE.replace("note", "@test_bot")


@pytest.mark.asyncio
async def test_rewrite_hook_replaces_the_verbatim_text_a_plugin_receives(home, monkeypatch):
    from plugins.platforms.discord.adapter import DiscordAdapter

    async def redact(name, **_kwargs):
        return [{"action": "rewrite", "text": "/log [redacted]"}] if name == "pre_gateway_dispatch" else []

    monkeypatch.setattr("hermes_cli.lifecycle.ainvoke_hook", redact)
    event = await _discord_event(DiscordAdapter(PlatformConfig(enabled=True, token="fake")), "/log  secret \n")
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    rewritten = await runner._hm_pre_gateway_dispatch_hook(event, event.source)
    assert rewritten is not event and rewritten.text == "/log [redacted]"
    assert (await runner._hm_dispatch_plugin_command(rewritten, rewritten.source, rewritten.get_command()))[0]
    assert (home / "captured.txt").read_text() == "[redacted]"
