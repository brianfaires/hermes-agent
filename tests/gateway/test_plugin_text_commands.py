"""Messaging commands preserve text-mode plugin payloads (#130)."""
import hermes_bootstrap
import asyncio

from pathlib import Path
from datetime import datetime, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import SessionSource
from hermes_cli.plugins import get_plugin_commands
from tests.hermes_cli.test_plugin_text_commands import install


@pytest.mark.asyncio
@pytest.mark.parametrize("platform, command", [(Platform.DISCORD, "log"), (Platform.TELEGRAM, "log@TestBot")])
async def test_gateway_preserves_text_while_options_keep_dash_repair(tmp_path, monkeypatch, platform, command):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with _profile_runtime_scope(tmp_path):
        install(tmp_path)
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig()
        source = SessionSource(platform=platform, chat_id="notes", user_id="owner", chat_type="dm")
        note = " café — note\n\n    pasted – line\nend \n"
        event = MessageEvent(text=f"/{command} " + note, source=source)
        handled, result, _ = await runner._hm_dispatch_plugin_command(event, source, event.get_command())
        assert handled and result == "Logged"
        assert (tmp_path / "captured.txt").read_text() == note
        options = MessageEvent(text="/options-example  —flag \n", source=source)
        await runner._hm_dispatch_plugin_command(options, source, options.get_command())
        assert (tmp_path / "captured.txt").read_text() == "--flag"


@pytest.mark.asyncio
async def test_telegram_split_note_keeps_the_continuation_whitespace(tmp_path, monkeypatch):
    from tests.gateway.test_telegram_long_command_batching import _make_adapter, _make_update

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with _profile_runtime_scope(tmp_path):
        install(tmp_path)
        adapter = _make_adapter()
        first = "/log " + "x" * 4090
        tail = "  next — line\n\n    indented\nend \n"
        try:
            await adapter._handle_command(_make_update(first), SimpleNamespace())
            await adapter._handle_text_message(_make_update(tail), SimpleNamespace())
            key = next(iter(adapter._pending_text_batches))
            await adapter._flush_text_batch_now(key)
            event = adapter.handle_message.call_args.args[0]
            runner = object.__new__(GatewayRunner)
            runner.config = GatewayConfig()
            assert (await runner._hm_dispatch_plugin_command(event, event.source, event.get_command()))[0]
            assert (tmp_path / "captured.txt").read_text() == "x" * 4090 + "\n" + tail
        finally:
            tasks = list(adapter._pending_text_batch_tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_telegram_addressed_text_survives_native_command_ingress(tmp_path, monkeypatch):
    from tests.gateway.test_telegram_long_command_batching import _make_adapter, _make_update

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with _profile_runtime_scope(tmp_path):
        install(tmp_path)
        adapter = _make_adapter()
        note = " café — @test_bot\n\n    pasted line\nend \n"
        update = _make_update("/log@test_bot " + note)
        update.message.entities = [SimpleNamespace(type="text_link", url="https://example.test/report",
                                                  offset=update.message.text.index("café"), length=4)]
        with _profile_runtime_scope(tmp_path / "transport"):
            assert "log" not in get_plugin_commands()
            await adapter._handle_command(update, SimpleNamespace())
        event = adapter.handle_message.call_args.args[0]
        assert "https://example.test/report" in event.text
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig()
        assert (await runner._hm_dispatch_plugin_command(event, event.source, event.get_command()))[0]
        assert (tmp_path / "captured.txt").read_text() == note.replace("café", "café (https://example.test/report)")
        # Option commands keep the adapter's useful link expansion.
        options = _make_update("/options-example label")
        options.message.entities = [SimpleNamespace(type="text_link", url="https://example.test/report",
                                                   offset=len("/options-example "), length=5)]
        with _profile_runtime_scope(tmp_path / "transport"):
            await adapter._handle_command(options, SimpleNamespace())
        option_event = adapter.handle_message.call_args.args[0]
        await runner._hm_dispatch_plugin_command(option_event, option_event.source, option_event.get_command())
        assert (tmp_path / "captured.txt").read_text() == "label (https://example.test/report)"
        adapter.handle_message.reset_mock()
        adapter._is_user_authorized_from_message = lambda _message: False
        await adapter._handle_command(_make_update("/log@test_bot " + note), SimpleNamespace())
        adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_registered_slash_preserves_the_full_text_argument(tmp_path, monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tests.gateway.test_discord_slash_commands import FakeTree

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with _profile_runtime_scope(tmp_path):
        install(tmp_path)
        adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
        adapter._client = SimpleNamespace(tree=FakeTree(), user=SimpleNamespace(id=99999))
        adapter._run_simple_slash = AsyncMock()
        adapter._register_slash_commands()
        note = " café — note\n\n    pasted line\nend \n"
        command = adapter._client.tree.commands["log"]
        await command.callback(SimpleNamespace(), args=note)
        adapter._run_simple_slash.assert_awaited_once_with(SimpleNamespace(), "/log " + note)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "<@99999> ", "<@!99999> "])
async def test_discord_message_ingress_preserves_mentions_inside_notes(tmp_path, monkeypatch, prefix):
    import discord
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with _profile_runtime_scope(tmp_path):
        install(tmp_path)
        command = "/log  café — <@99999>\n\n    pasted line\nend \n"
        adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake"))
        bot = SimpleNamespace(id=99999, name="Hermes", bot=True)
        adapter._client = SimpleNamespace(user=bot)
        events = []
        adapter.handle_message = AsyncMock(side_effect=events.append)
        channel = Mock(spec=discord.DMChannel)
        channel.id = 7
        message = SimpleNamespace(
            id=42, content=prefix + command, channel=channel, guild=None,
            author=SimpleNamespace(id=2, name="owner", display_name="Owner", bot=False),
            mentions=[bot], message_snapshots=[], attachments=[], reference=None,
            created_at=datetime.now(UTC),
        )
        with _profile_runtime_scope(tmp_path / "transport"):
            assert "log" not in get_plugin_commands()
            assert await adapter._handle_message(message)
        assert [event.text for event in events] == [command]
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig()
        event = events[0]
        assert (await runner._hm_dispatch_plugin_command(event, event.source, event.get_command()))[0]
        assert (tmp_path / "captured.txt").read_text() == command[len("/log "):]
