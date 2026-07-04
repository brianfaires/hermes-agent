"""Conversational speech preservation during context compression."""

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


def test_visible_stt_content_survives_without_promoting_metadata_transcripts():
    comp = _make_compressor()

    serialized = comp._serialize_for_summary([
        {
            "role": "user",
            "content": "turn on the kitchen lights",
            "metadata": {"transcript": "unrelated metadata transcript"},
        },
    ])

    assert "turn on the kitchen lights" in serialized
    assert "unrelated metadata transcript" not in serialized


def test_tts_tool_text_survives_without_delivery_artifacts():
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

    assert spoken in serialized
    assert "text_to_speech" in serialized
    assert "/tmp/tts_reply.mp3" not in serialized


def test_matching_visible_and_tts_text_is_serialized_once():
    comp = _make_compressor()
    spoken = "The backup finished successfully."

    serialized = comp._serialize_for_summary([
        {"role": "assistant", "content": spoken, "tool_calls": [_tts_call("call_tts", spoken)]},
    ])

    assert serialized.count(spoken) == 1
    assert "/tmp/tts_reply.mp3" not in serialized


def test_long_tts_text_survives_precompression_tool_argument_pruning():
    comp = _make_compressor()
    spoken = "The backup is still running. " * 30
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [_tts_call("call_tts", spoken)]},
    ]

    pruned, _count = comp._prune_old_tool_results(messages, protect_tail_count=0)
    args = json.loads(pruned[0]["tool_calls"][0]["function"]["arguments"])

    assert args["text"] == spoken


def test_static_fallback_preserves_tts_text_after_long_visible_content():
    comp = _make_compressor()
    spoken = "The backup finished successfully."

    summary = comp._build_static_fallback_summary([
        {"role": "user", "content": "start the backup"},
        {
            "role": "assistant",
            "content": "Diagnostic details. " * 80,
            "tool_calls": [_tts_call("call_tts", spoken)],
        },
        {
            "role": "tool",
            "tool_call_id": "call_tts",
            "content": '{"success": true, "file_path": "/tmp/tts_reply.mp3"}',
        },
    ])

    assert "start the backup" in summary
    assert spoken in summary
    assert "/tmp/tts_reply.mp3" not in summary
