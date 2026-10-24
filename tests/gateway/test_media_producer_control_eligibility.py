"""Literal model text cannot suppress explicit current-turn artifact delivery."""
from gateway import run
from gateway.run_turn_runner import TurnRunner
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.api_server_media import resolve_media_to_data_urls


def _result(path, tool='text_to_speech'):
    return {'messages': [
        {'role': 'assistant', 'tool_calls': [{'id': 'producer', 'function': {'name': tool}}]},
        {'role': 'tool', 'tool_call_id': 'producer', 'content': '[[audio_as_voice]]\nMEDIA:' + str(path)},
    ]}


def test_literal_example_does_not_suppress_authorized_tts_artifact(tmp_path):
    audio = tmp_path / 'voice.ogg'
    audio.write_bytes(b'synthetic audio')
    prose = 'For example MEDIA:' + str(tmp_path / 'example.png')
    out = TurnRunner._append_auto_media_tags(None, prose, _result(audio), [], set())
    media, cleaned = BasePlatformAdapter.extract_media(out)
    assert media == [(str(audio), True)]
    assert cleaned == prose


def test_explicit_control_and_untrusted_tool_examples_keep_native_boundaries(tmp_path):
    audio = tmp_path / 'voice.ogg'
    audio.write_bytes(b'synthetic audio')
    explicit = 'MEDIA:' + str(audio)
    assert TurnRunner._append_auto_media_tags(None, explicit, _result(audio), [], set()) == explicit
    prose = 'For example MEDIA:' + str(tmp_path / 'example.png')
    assert TurnRunner._append_auto_media_tags(None, prose, _result(audio, 'read_file'), [], set()) == prose


def test_api_invalid_media_path_is_nonfatal():
    text = 'MEDIA:/synthetic/invalid\x00.png'
    assert resolve_media_to_data_urls(text) == text
