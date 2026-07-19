import json
from unittest.mock import MagicMock, patch

import pytest
import yaml

from gateway.voice_acknowledgements import (
    VoiceAcknowledgement,
    VoiceAcknowledgementCatalog,
)


def _write_catalog(path, acknowledgements):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "profile": "test",
                "defaults": {
                    "enabled": True,
                    "weight": 1,
                    "models": {"include": ["*"], "exclude": []},
                    "voice": {"style": None, "stability": None, "speed": None},
                },
                "acknowledgements": acknowledgements,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_catalog_merges_groups_phrase_overrides_and_llm_filters(tmp_path):
    path = tmp_path / "voice" / "acknowledgements.yaml"
    _write_catalog(
        path,
        {
            "model_switch": {
                "groups": {
                    "general": {"phrases": ["Loaded [name]."]},
                    "sol": {
                        "models": {"include": ["gpt-5.6-sol"]},
                        "weight": 3,
                        "voice": {"style": 0.2, "speed": 1.1},
                        "phrases": [
                            {"text": "Smarter now.", "voice": {"speed": 1.05}}
                        ],
                    },
                }
            }
        },
    )
    catalog = VoiceAcknowledgementCatalog.load(path)

    with patch(
        "gateway.voice_acknowledgements.random.choices",
        side_effect=lambda population, weights, k: [
            next(item for item in population if item.text == "Smarter now.")
        ],
    ):
        selected = catalog.choose("model_switch", model_name="openai/gpt-5.6-sol")

    assert selected.text == "Smarter now."
    assert selected.weight == 3
    assert selected.voice_settings == {"style": 0.2, "speed": 1.05}
    assert [
        item.text
        for item in catalog.eligible("model_switch", model_name="gpt-5.6-luna")
    ] == ["Loaded [name]."]


def test_catalog_honors_disabled_groups_exclusions_and_weights(tmp_path):
    path = tmp_path / "voice" / "acknowledgements.yaml"
    _write_catalog(
        path,
        {
            "busy": {
                "groups": {
                    "disabled": {"enabled": False, "phrases": ["Nope."]},
                    "active": {
                        "models": {"exclude": ["gpt-5.6-luna"]},
                        "phrases": [
                            "Still working.",
                            {"text": "Nearly there.", "weight": 4},
                        ],
                    },
                }
            }
        },
    )
    catalog = VoiceAcknowledgementCatalog.load(path)

    assert catalog.eligible("busy", model_name="gpt-5.6-luna") == ()
    eligible = catalog.eligible("busy", model_name="gpt-5.6-sol")
    assert [(item.text, item.weight) for item in eligible] == [
        ("Still working.", 1),
        ("Nearly there.", 4),
    ]


def test_catalog_is_cached_after_load_and_invalid_files_fail_soft(tmp_path):
    path = tmp_path / "voice" / "acknowledgements.yaml"
    _write_catalog(
        path,
        {"tool_call": {"groups": {"general": {"phrases": ["On it."]}}}},
    )
    catalog = VoiceAcknowledgementCatalog.load(path)
    path.write_text("not: [valid", encoding="utf-8")

    assert catalog.choose("tool_call", model_name="gpt-5.6-sol").text == "On it."
    assert not VoiceAcknowledgementCatalog.load(tmp_path / "missing.yaml")

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("schema_version: 99\n", encoding="utf-8")
    assert not VoiceAcknowledgementCatalog.load(invalid)


@pytest.mark.asyncio
async def test_tool_ack_passes_catalog_voice_settings_to_tts(tmp_path):
    from tests.gateway.test_discord_voice_mixer import _make_adapter, vm

    adapter = _make_adapter()
    adapter._voice_mixers[111] = MagicMock()
    adapter._reset_voice_timeout = MagicMock()
    adapter._voice_ack_catalog = MagicMock()
    adapter._voice_ack_catalog.choose.return_value = VoiceAcknowledgement(
        "[impatient] Working!",
        1,
        {"style": 0.2, "stability": 0.5, "speed": 1.1},
        ("gpt-5.6-sol",),
        (),
    )
    ack_file = tmp_path / "ack.mp3"
    ack_file.write_bytes(b"id3")
    seen = {}

    def fake_tts(**kwargs):
        seen.update(kwargs)
        return json.dumps({"success": True, "file_path": str(ack_file)})

    with patch("tools.tts_tool.text_to_speech_tool", side_effect=fake_tts), patch.object(
        vm, "decode_to_pcm", return_value=b"\x00" * vm.FRAME_SIZE
    ):
        assert await adapter.play_ack_in_voice(
            111, model_name="openai/gpt-5.6-sol"
        )

    assert seen["text"] == "[impatient] Working!"
    assert seen["voice_settings"] == {
        "style": 0.2,
        "stability": 0.5,
        "speed": 1.1,
    }


def test_per_utterance_tts_settings_do_not_mutate_profile(tmp_path, monkeypatch):
    from tools import tts_tool

    output_path = tmp_path / "out.mp3"
    config = {
        "provider": "elevenlabs",
        "elevenlabs": {
            "style": 0.8,
            "stability": 0.4,
            "speed": 1.1,
            "similarity_boost": 0.85,
        },
    }
    captured = {}
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: config)
    monkeypatch.setattr(tts_tool, "_import_elevenlabs", lambda: object)

    def fake_generate(_text, out, resolved_config):
        captured.update(resolved_config)
        output_path.write_bytes(b"audio")
        return out

    monkeypatch.setattr(tts_tool, "_generate_elevenlabs", fake_generate)
    result = json.loads(
        tts_tool.text_to_speech_tool(
            "hello",
            str(output_path),
            voice_settings={"style": 0.1, "speed": 1.05},
        )
    )

    assert result["success"] is True
    assert captured["elevenlabs"] == {
        "style": 0.1,
        "stability": 0.4,
        "speed": 1.05,
        "similarity_boost": 0.85,
    }
    assert config["elevenlabs"]["style"] == 0.8
    assert config["elevenlabs"]["speed"] == 1.1


def test_eleven_v3_tags_survive_directed_tts_cleanup():
    from tools.tts_tool import _strip_markdown_for_tts

    assert _strip_markdown_for_tts("[sarcastic] Working!") == "[sarcastic] Working!"
