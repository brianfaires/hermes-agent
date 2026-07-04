"""Voice/STT/TTS preservation during context compression."""

import json
from unittest.mock import patch

from agent.context_compressor import ContextCompressor


def _make_compressor() -> ContextCompressor:
    with patch("agent.context_compressor.get_model_context_length", return_value=8000):
        return ContextCompressor(
            model="test-model",
            threshold_percent=0.75,
            protect_first_n=3,
            protect_last_n=4,
            quiet_mode=True,
        )


def _tts_call(call_id: str, text: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "text_to_speech",
            "arguments": json.dumps({"text": text, "output_path": "/tmp/tts_reply.mp3"}),
        },
    }


def test_voice_only_stt_metadata_is_serialized_as_user_content():
    comp = _make_compressor()

    serialized = comp._serialize_for_summary([
        {"role": "user", "content": "", "stt_transcript": "turn on the kitchen lights"},
    ])

    assert "[USER]: [Spoken input]: turn on the kitchen lights" in serialized


def test_spoken_input_not_duplicated_when_it_matches_visible_text():
    comp = _make_compressor()

    serialized = comp._serialize_for_summary([
        {
            "role": "user",
            "content": "Turn on the kitchen lights.",
            "stt_transcript": "turn on the kitchen lights",
        },
    ])

    assert serialized.count("turn on the kitchen lights") == 0
    assert serialized.count("Turn on the kitchen lights.") == 1
    assert "[Spoken input]" not in serialized


def test_tts_alternate_output_is_serialized_as_assistant_content():
    comp = _make_compressor()

    serialized = comp._serialize_for_summary([
        {
            "role": "assistant",
            "content": "Long diagnostic report is attached below.",
            "tts_text": "I found one warning. Details are in the text follow-up.",
        },
    ])

    assert "Long diagnostic report is attached below." in serialized
    assert "[Spoken assistant output]: I found one warning" in serialized


def test_tts_tool_call_preserves_spoken_text_but_excludes_audio_artifact_result():
    comp = _make_compressor()
    spoken = "The backup finished successfully."

    serialized = comp._serialize_for_summary([
        {"role": "assistant", "content": "", "tool_calls": [_tts_call("call_tts", spoken)]},
        {
            "role": "tool",
            "tool_call_id": "call_tts",
            "content": '{"success": true, "file_path": "/tmp/tts_reply.mp3", "duration": 1.2}',
        },
    ])

    assert "[Spoken assistant output]: The backup finished successfully." in serialized
    assert "text_to_speech({})" in serialized
    assert "[text_to_speech] generated audio" in serialized
    assert "/tmp/tts_reply.mp3" not in serialized


def test_matching_tts_tool_text_is_not_duplicated_when_visible_text_exists():
    comp = _make_compressor()
    spoken = "The backup finished successfully."

    serialized = comp._serialize_for_summary([
        {"role": "assistant", "content": spoken, "tool_calls": [_tts_call("call_tts", spoken)]},
    ])

    assert serialized.count(spoken) == 1
    assert "[Spoken assistant output]" not in serialized
    assert "text_to_speech({})" in serialized
    assert "/tmp/tts_reply.mp3" not in serialized


def test_static_fallback_preserves_voice_content_and_excludes_tts_artifact_path():
    comp = _make_compressor()
    spoken = "The backup finished successfully."

    summary = comp._build_static_fallback_summary([
        {"role": "user", "content": "", "stt_transcript": "start the backup"},
        {"role": "assistant", "content": "", "tool_calls": [_tts_call("call_tts", spoken)]},
        {
            "role": "tool",
            "tool_call_id": "call_tts",
            "content": '{"success": true, "file_path": "/tmp/tts_reply.mp3"}',
        },
    ])

    assert "[Spoken input]: start the backup" in summary
    assert "[Spoken assistant output]: The backup finished successfully." in summary
    assert "/tmp/tts_reply.mp3" not in summary
