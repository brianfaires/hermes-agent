"""Gateway voice-mode handling for clarify prompts."""

from unittest.mock import MagicMock, patch

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _runner() -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._voice_mode = {}
    return runner


def _event(message_type: MessageType = MessageType.TEXT) -> MessageEvent:
    return MessageEvent(
        text="trigger",
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="123",
            user_id="u1",
            user_name="User",
        ),
        message_type=message_type,
        message_id="m1",
    )


def test_clarify_voice_prompt_format_reads_choices_and_other_option():
    runner = _runner()

    spoken = runner._format_clarify_prompt_for_tts(
        "Which environment?",
        ["staging", "production"],
    )

    assert spoken == (
        "I need a choice: Which environment? "
        "Option 1: staging. Option 2: production. "
        "Or say another answer."
    )


def test_clarify_voice_prompt_format_handles_open_ended_question():
    runner = _runner()

    assert runner._format_clarify_prompt_for_tts("What changed?", None) == (
        "I need clarification: What changed?"
    )


def test_clarify_voice_prompt_not_scheduled_when_voice_mode_off():
    runner = _runner()
    event = _event(MessageType.VOICE)

    with patch("gateway.voice_mixin.safe_schedule_threadsafe") as schedule:
        scheduled = runner._maybe_send_clarify_voice_prompt(
            event=event,
            question="Pick one?",
            choices=["A", "B"],
            loop=MagicMock(),
        )

    assert scheduled is False
    schedule.assert_not_called()


def test_clarify_voice_prompt_scheduled_for_all_mode_text_input():
    runner = _runner()
    event = _event(MessageType.TEXT)
    runner._voice_mode[runner._voice_key(Platform.DISCORD, "123")] = "all"

    runner._send_voice_reply = MagicMock()
    with patch("gateway.voice_mixin.safe_schedule_threadsafe", return_value=MagicMock()) as schedule:
        scheduled = runner._maybe_send_clarify_voice_prompt(
            event=event,
            question="Pick one?",
            choices=["A", "B"],
            loop=MagicMock(),
        )

    assert scheduled is True
    schedule.assert_called_once()
    runner._send_voice_reply.assert_called_once_with(
        event,
        "I need a choice: Pick one? Option 1: A. Option 2: B. Or say another answer.",
    )


def test_clarify_voice_prompt_scheduled_for_voice_only_mode_after_voice_input():
    runner = _runner()
    event = _event(MessageType.VOICE)
    runner._voice_mode[runner._voice_key(Platform.DISCORD, "123")] = "voice_only"

    runner._send_voice_reply = MagicMock()
    with patch("gateway.voice_mixin.safe_schedule_threadsafe", return_value=MagicMock()) as schedule:
        scheduled = runner._maybe_send_clarify_voice_prompt(
            event=event,
            question="Pick one?",
            choices=["A", "B"],
            loop=MagicMock(),
        )

    assert scheduled is True
    schedule.assert_called_once()
    runner._send_voice_reply.assert_called_once_with(
        event,
        "I need a choice: Pick one? Option 1: A. Option 2: B. Or say another answer.",
    )


def test_clarify_voice_prompt_not_scheduled_for_voice_only_mode_after_text_input():
    runner = _runner()
    event = _event(MessageType.TEXT)
    runner._voice_mode[runner._voice_key(Platform.DISCORD, "123")] = "voice_only"

    with patch("gateway.voice_mixin.safe_schedule_threadsafe") as schedule:
        scheduled = runner._maybe_send_clarify_voice_prompt(
            event=event,
            question="Pick one?",
            choices=["A", "B"],
            loop=MagicMock(),
        )

    assert scheduled is False
    schedule.assert_not_called()


def test_approval_voice_prompt_format_does_not_read_full_command():
    runner = _runner()

    spoken = runner._format_approval_prompt_for_tts("recursive delete")

    assert spoken == (
        "Command approval needed. Reason: recursive delete. "
        "Use the approval buttons, or say approve or deny."
    )


def test_approval_voice_prompt_scheduled_for_all_mode_text_input():
    runner = _runner()
    event = _event(MessageType.TEXT)
    runner._voice_mode[runner._voice_key(Platform.DISCORD, "123")] = "all"

    runner._send_voice_reply = MagicMock()
    with patch("gateway.voice_mixin.safe_schedule_threadsafe", return_value=MagicMock()) as schedule:
        scheduled = runner._maybe_send_approval_voice_prompt(
            event=event,
            description="dangerous command",
            loop=MagicMock(),
        )

    assert scheduled is True
    schedule.assert_called_once()
    runner._send_voice_reply.assert_called_once_with(
        event,
        "Command approval needed. Reason: dangerous command. "
        "Use the approval buttons, or say approve or deny.",
    )


def test_approval_voice_prompt_not_scheduled_when_voice_mode_off():
    runner = _runner()
    event = _event(MessageType.VOICE)

    with patch("gateway.voice_mixin.safe_schedule_threadsafe") as schedule:
        scheduled = runner._maybe_send_approval_voice_prompt(
            event=event,
            description="dangerous command",
            loop=MagicMock(),
        )

    assert scheduled is False
    schedule.assert_not_called()
