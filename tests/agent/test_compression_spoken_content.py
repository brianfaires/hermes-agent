"""Spoken conversation survives serialization and deterministic recovery."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Settle native update recovery before the per-test live-home I/O guard.
import hermes_bootstrap  # noqa: F401

from agent.context_compressor import ContextCompressor


def _compressor():
    with patch('agent.context_compressor.get_model_context_length', return_value=100000):
        compressor = ContextCompressor(model='test/model', quiet_mode=True)
        _ = compressor.context_length
    return compressor


def _turns(text, shape):
    function = {'name': 'text_to_speech', 'arguments': json.dumps({
        'text': text, 'output_path': '/synthetic/delivery-only.wav'})}
    call = {'id': 'speech', 'type': 'function', 'function': function}
    if shape == 'object':
        call = SimpleNamespace(id='speech', type='function', function=SimpleNamespace(**function))
    return [{'role': 'assistant', 'content': '', 'tool_calls': [call]},
            {'role': 'tool', 'tool_call_id': 'speech',
             'content': 'MEDIA:/synthetic/delivery-only.wav duration=999'}]


@pytest.mark.parametrize('shape', ['dict', 'object'])
def test_long_spoken_arguments_survive_pruning_and_serialization(shape):
    compressor = _compressor()
    speech = 'A spoken start. ' + 'ordinary speech ' * 150 + ' Crucial spoken ending.'
    turns = _turns(speech, shape)
    before = copy.deepcopy(turns)
    pruned, _ = compressor._prune_old_tool_results(turns, protect_tail_count=0)
    serialized = compressor._serialize_for_summary(pruned)
    assert speech in serialized
    assert '/synthetic/delivery-only.wav' not in serialized
    assert 'duration=999' not in serialized
    assert turns == before


def test_fallback_keeps_speech_without_artifacts_and_visible_duplicate():
    compressor = _compressor()
    turns = [{'role': 'user', 'content': 'Remember the blue plan.'}, *_turns('We chose the blue plan.', 'dict')]
    fallback = compressor._build_static_fallback_summary(turns, reason='synthetic unavailable provider')
    assert 'We chose the blue plan.' in fallback
    assert 'delivery-only.wav' not in fallback
    assert 'duration=999' not in fallback
    turns[1]['content'] = 'We chose the **blue** plan!'
    serialized = compressor._serialize_for_summary(turns)
    assert '[Spoken assistant output]' not in serialized
    assert 'We chose the **blue** plan!' in serialized
