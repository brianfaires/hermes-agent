"""Tests for long-running gateway progress heartbeat policy."""

from gateway.long_running_progress import (
    heartbeat_interval_seconds,
    heartbeat_text,
    should_send_voice_heartbeat,
)
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, SessionSource


def _event(platform=Platform.DISCORD, message_type=MessageType.VOICE):
    return MessageEvent(
        text="debug this",
        message_type=message_type,
        source=SessionSource(
            platform=platform,
            chat_id="chat-1",
            user_id="user-1",
        ),
    )


def test_heartbeat_interval_defaults_to_two_minutes():
    assert heartbeat_interval_seconds({}, platform_key="discord") == 120.0


def test_heartbeat_interval_uses_agent_config():
    config = {"agent": {"gateway_notify_interval": 45}}

    assert heartbeat_interval_seconds(config, platform_key="discord") == 45.0


def test_heartbeat_interval_can_be_disabled():
    config = {"agent": {"gateway_notify_interval": 0}}

    assert heartbeat_interval_seconds(config, platform_key="discord") is None


def test_heartbeat_text_escalates_after_ten_minutes():
    early = heartbeat_text(elapsed_seconds=130)
    late = heartbeat_text(elapsed_seconds=610)

    assert "2 min" in early
    assert "Still working" in early
    assert "I don’t have a clean result yet" in late
    assert "10 min" in late


def test_voice_heartbeat_only_for_discord_voice_turns():
    assert should_send_voice_heartbeat(_event()) is True
    assert should_send_voice_heartbeat(_event(message_type=MessageType.TEXT)) is False
    assert should_send_voice_heartbeat(_event(platform=Platform.TELEGRAM)) is False


def test_text_transcript_channel_turn_does_not_get_spoken_heartbeat():
    event = MessageEvent(
        text="after restart, answer in text",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="1517206455512731961",
            user_id="user-1",
        ),
    )

    assert should_send_voice_heartbeat(event) is False
