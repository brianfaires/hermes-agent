"""Messaging commands preserve text-mode plugin payloads (#130)."""
from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import SessionSource
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


def test_runner_command_finds_text_plugin_case_insensitively(tmp_path):
    from gateway.session_commands import _runner_command

    with _profile_runtime_scope(tmp_path):
        install(tmp_path)
        assert _runner_command("Log") == ("plugin", "log")
        assert _runner_command("Capture_Note") == ("plugin", "capture_note")
