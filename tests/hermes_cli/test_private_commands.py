"""Private plugin commands stay outside history, observers and model sessions."""
from unittest.mock import AsyncMock, Mock

from cli import HermesCLI
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import SessionSource
from hermes_cli.plugins import get_plugin_manager

import pytest


def _install(home):
    plugin = home / 'plugins' / 'private-example'
    plugin.mkdir(parents=True)
    (plugin / 'plugin.yaml').write_text('name: private-example\nversion: 1.0.0\n')
    (plugin / '__init__.py').write_text(
        'def register(ctx):\n'
        '    def capture(raw, *, home):\n'
        '        (home / "captured.txt").write_text(raw)\n'
        '        return "Captured"\n'
        '    ctx.register_command("private-example", capture, private=True)\n')
    (home / 'config.yaml').write_text('plugins:\n  enabled: [private-example]\n')
    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    loaded = manager._plugins['private-example']
    assert loaded.enabled and not loaded.error, loaded.error
    return manager


def test_private_cli_is_profile_local_and_skips_input_history(tmp_path):
    from hermes_cli.private_commands import PrivateFileHistory

    for label in ('a', 'b'):
        home = tmp_path / label
        with _profile_runtime_scope(home):
            _install(home)
            cli = object.__new__(HermesCLI)
            assert cli.process_command('/private-example  ' + label + '\n ')
            assert (home / 'captured.txt').read_text() == ' ' + label + '\n '
            history = PrivateFileHistory(str(home / 'history'))
            history.append_string('/private-example hidden body')
            history.append_string('ordinary next')
            assert history.get_strings() == ['ordinary next']
            assert 'hidden body' not in (home / 'history').read_text()


@pytest.mark.asyncio
async def test_private_gateway_authorizes_before_busy_startup_and_observers(tmp_path):
    home = tmp_path / 'owner'
    with _profile_runtime_scope(home):
        manager = _install(home)
        observer = Mock()
        manager._hooks.setdefault('pre_gateway_dispatch', []).append(observer)
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig()
        runner._is_user_authorized_for_source = Mock(return_value=False)
        runner._check_slash_access = Mock(return_value=None)
        runner._resolve_profile_home_for_source = Mock(return_value=home)
        runner._startup_restore_in_progress = True
        runner._queue_startup_restore_event = Mock(side_effect=AssertionError('startup queue'))
        runner._running_agents = {'busy': Mock()}
        runner.session_store = Mock()
        runner._handle_message_with_agent = AsyncMock(side_effect=AssertionError('model turn'))
        event = MessageEvent(text='/private-example secret', source=SessionSource(
            platform=Platform.TELEGRAM, chat_id='100', user_id='1'))
        assert await runner._handle_message(event) is None
        assert not (home / 'captured.txt').exists()
        runner._is_user_authorized_for_source.return_value = True
        assert await runner._handle_message(MessageEvent(text='/private-example secret',
            source=event.source)) == 'Captured'
        assert (home / 'captured.txt').read_text() == 'secret'
        observer.assert_not_called()
        assert not runner.session_store.mock_calls


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed,ignored,mention,free,user_allowed,expected', [
    (set(), set(), False, set(), True, True),
    ({'other'}, set(), False, set(), True, False),
    ({'100'}, set(), False, set(), True, True),
    (set(), {'100'}, False, set(), True, False),
    (set(), {'*'}, False, set(), True, False),
    (set(), set(), True, set(), True, False),
    (set(), set(), True, {'100'}, True, True),
    (set(), set(), False, set(), False, False),
])
async def test_private_discord_live_dispatch_preserves_admission(
    tmp_path, allowed, ignored, mention, free, user_allowed, expected,
):
    """The actual live dispatcher claims dedup before invoking the private handler."""
    from types import SimpleNamespace
    discord = pytest.importorskip('discord')
    from gateway.config import PlatformConfig
    from plugins.platforms.discord.adapter import DiscordAdapter

    home = tmp_path / 'owner'
    with _profile_runtime_scope(home):
        _install(home)
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig()
        runner._is_user_authorized_for_source = Mock(return_value=True)
        runner._check_slash_access = Mock(return_value=None)
        runner._resolve_profile_home_for_source = Mock(return_value=home)
        runner._startup_restore_in_progress = True
        runner._queue_startup_restore_event = Mock(side_effect=AssertionError('startup queue'))
        adapter = DiscordAdapter(PlatformConfig(enabled=True, token='synthetic'))
        adapter._client = SimpleNamespace(user=SimpleNamespace(id=9))
        adapter._ready_event.set()
        adapter._is_allowed_user = Mock(return_value=user_allowed)
        adapter._get_allowed_channels = lambda: allowed
        adapter._get_ignored_channels = lambda: ignored
        adapter._discord_require_mention = lambda: mention
        adapter._discord_free_response_channels = lambda: free
        adapter._message_handler = runner._handle_message
        adapter._send_with_retry = AsyncMock()
        adapter._handle_conversational_message = AsyncMock(side_effect=AssertionError('conversation'))
        message = SimpleNamespace(
            id=7, content='/private-example synthetic capture', mentions=[],
            channel=SimpleNamespace(id=100, name='fixture'),
            guild=SimpleNamespace(id=2, name='Guild'),
            author=SimpleNamespace(id=1, display_name='Fixture', name='Fixture', bot=False),
            type=discord.MessageType.default, attachments=[],
        )
        assert await adapter._dispatch_discord_message(message) is expected
        assert (home / 'captured.txt').exists() is expected
        assert not await adapter._dispatch_discord_message(message)
        adapter._handle_conversational_message.assert_not_called()
        assert adapter._send_with_retry.await_count == int(expected)
