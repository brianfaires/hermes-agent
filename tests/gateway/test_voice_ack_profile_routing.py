from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.voice_acknowledgements import (
    VoiceAcknowledgement,
    VoiceAcknowledgementCatalog,
)


def test_adapter_for_source_prefers_secondary_profile_adapter():
    runner = object.__new__(GatewayRunner)
    runner._gateway_profile_name = "default"
    primary = SimpleNamespace(_running=True)
    secondary = SimpleNamespace(_running=True)
    runner.adapters = {Platform.DISCORD: primary}
    runner._profile_adapters = {"ops": {Platform.DISCORD: secondary}}

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        profile="ops",
    )

    assert runner._adapter_for_source(source) is secondary


def test_model_switch_uses_secondary_profiles_ack_catalog():
    runner = object.__new__(GatewayRunner)
    runner._gateway_profile_name = "default"
    primary_catalog = VoiceAcknowledgementCatalog(
        {
            "model_switch": [
                VoiceAcknowledgement("Primary [name].", 1, {}, ("*",), ())
            ]
        }
    )
    secondary_catalog = VoiceAcknowledgementCatalog(
        {
            "model_switch": [
                VoiceAcknowledgement("Secondary [name].", 1, {}, ("*",), ())
            ]
        }
    )
    runner.adapters = {
        Platform.DISCORD: SimpleNamespace(
            _running=True,
            _voice_fx_cfg={},
            _voice_ack_catalog=primary_catalog,
        )
    }
    runner._profile_adapters = {
        "ops": {
            Platform.DISCORD: SimpleNamespace(
                _running=True,
                _voice_fx_cfg={},
                _voice_ack_catalog=secondary_catalog,
            )
        }
    }
    event = MessageEvent(
        text="/model gpt-5.6-sol",
        message_type=MessageType.VOICE,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="123",
            profile="ops",
        ),
    )

    selected = runner._model_switch_voice_ack(
        event,
        model_name="openai/gpt-5.6-sol",
        command_model_name="gpt-5.6-sol",
    )

    assert selected.text == "Secondary gpt-5.6-sol."


@pytest.mark.asyncio
async def test_stop_voice_playback_uses_secondary_profile_adapter():
    runner = object.__new__(GatewayRunner)
    runner._gateway_profile_name = "default"

    class Adapter:
        def __init__(self):
            self._running = True
            self.guild_ids = []

        async def stop_voice_playback(self, guild_id):
            self.guild_ids.append(guild_id)
            return True

    primary = Adapter()
    secondary = Adapter()
    runner.adapters = {Platform.DISCORD: primary}
    runner._profile_adapters = {"ops": {Platform.DISCORD: secondary}}
    event = MessageEvent(
        text="/stop",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="123",
            profile="ops",
        ),
    )
    event.raw_message = SimpleNamespace(guild_id="12345")

    assert await runner._stop_voice_playback_for_event(event) is True
    assert primary.guild_ids == []
    assert secondary.guild_ids == [12345]
