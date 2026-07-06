"""Tests for safe TTS diagnostic logging."""

import json
import logging


def test_elevenlabs_success_log_includes_profile_and_voice_details_without_secrets(
    tmp_path,
    monkeypatch,
    caplog,
):
    from tools import tts_tool

    output_path = tmp_path / "out.mp3"
    profile_home = tmp_path / ".hermes" / "profiles" / "ops"
    config = {
        "provider": "elevenlabs",
        "elevenlabs": {
            "voice_id": "voice-123",
            "model_id": "eleven-model",
            "speed": 1.1,
            "style": 0.2,
            "stability": 0.3,
            "similarity_boost": 0.4,
            "use_speaker_boost": True,
            "api_key": "should-not-be-logged",
        },
    }

    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: config)
    monkeypatch.setattr(tts_tool, "get_hermes_home", lambda: profile_home)
    monkeypatch.setattr(tts_tool, "display_hermes_home", lambda: "~/.hermes/profiles/ops")
    monkeypatch.setattr(tts_tool, "_import_elevenlabs", lambda: object)

    def fake_generate(_text, out, _config):
        with open(out, "wb") as handle:
            handle.write(b"audio")
        return out

    monkeypatch.setattr(tts_tool, "_generate_elevenlabs", fake_generate)

    with caplog.at_level(logging.INFO, logger="tools.tts_tool"):
        result = json.loads(tts_tool.text_to_speech_tool("hello", str(output_path)))

    assert result["success"] is True
    assert result["provider"] == "elevenlabs"

    log_text = caplog.text
    assert "TTS audio generated:" in log_text
    assert f"path={output_path}" in log_text
    assert "hermes_home='~/.hermes/profiles/ops'" in log_text
    assert "profile='ops'" in log_text
    assert "provider='elevenlabs'" in log_text
    assert "voice_id='voice-123'" in log_text
    assert "model_id='eleven-model'" in log_text
    assert "speed=1.1" in log_text
    assert "style=0.2" in log_text
    assert "stability=0.3" in log_text
    assert "similarity_boost=0.4" in log_text
    assert "use_speaker_boost=True" in log_text
    assert "should-not-be-logged" not in log_text
    assert "api_key" not in log_text


def test_active_hermes_profile_label_distinguishes_default(monkeypatch, tmp_path):
    from tools import tts_tool

    default_home = tmp_path / ".hermes"
    monkeypatch.setattr(tts_tool.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(tts_tool, "get_hermes_home", lambda: default_home)

    assert tts_tool._active_hermes_profile_label() == "default"
