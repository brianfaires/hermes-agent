from types import SimpleNamespace

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
