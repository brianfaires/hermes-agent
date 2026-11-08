"""Discord subclasses can configure the SDK transport before its first login."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from gateway.config import PlatformConfig
from tests.gateway.test_discord_connect import DiscordAdapter, FakeBot, discord_platform


@pytest.mark.asyncio
async def test_connect_uses_subclass_client_factory_before_login(monkeypatch):
    marker = object()
    observed = []

    class ConfiguredBot(FakeBot):
        def __init__(self, **options):
            self.transport_option = options.get('http_trace')
            super().__init__(**options)

        async def start(self, token):
            observed.append(self.transport_option)
            await super().start(token)

    class ExtendedDiscord(DiscordAdapter):
        def _build_client(self, **options):
            return ConfiguredBot(**{**options, 'http_trace': marker})

    adapter = ExtendedDiscord(PlatformConfig(enabled=True, token='synthetic'))
    monkeypatch.setattr(adapter, '_acquire_platform_lock', lambda *a, **kw: True)
    monkeypatch.setattr(adapter, '_release_platform_lock', lambda: None)
    monkeypatch.setattr(discord_platform.Intents, 'default', lambda: SimpleNamespace(
        message_content=False, dm_messages=False, guild_messages=False, members=False, voice_states=False))
    monkeypatch.setattr(discord_platform.commands, 'Bot', ConfiguredBot)
    monkeypatch.setattr(adapter, '_resolve_allowed_usernames', AsyncMock())
    try:
        assert await adapter.connect()
        assert observed == [marker]
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_join_voice_channel_uses_subclass_voice_client_factory():
    voice_client = SimpleNamespace(is_connected=lambda: True, channel=SimpleNamespace(id=7))
    connected = []

    class ExtendedDiscord(DiscordAdapter):
        async def _connect_voice_channel(self, channel):
            connected.append(channel)
            return voice_client

    adapter = ExtendedDiscord(PlatformConfig(enabled=True, token='synthetic'))
    adapter._client = SimpleNamespace()
    channel = SimpleNamespace(id=7, guild=SimpleNamespace(id=1),
                              connect=AsyncMock(side_effect=AssertionError('native connect used')))
    assert await adapter.join_voice_channel(channel)
    assert connected == [channel]
    assert adapter._voice_clients[1] is voice_client
