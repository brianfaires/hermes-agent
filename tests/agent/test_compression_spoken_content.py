"""Spoken conversation survives serialization and deterministic recovery."""
import copy
import json
from types import SimpleNamespace

import pytest

# Settle native update recovery before the per-test live-home I/O guard.
import hermes_bootstrap

from agent.context_compressor import ContextCompressor


def _compressor():
    return ContextCompressor(model='test/model', quiet_mode=True, config_context_length=131072)


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
    turns[1]['content'] = 'We chose the **blue** plan.'
    serialized = compressor._serialize_for_summary(turns)
    assert '[Spoken assistant output]' not in serialized
    assert 'We chose the **blue** plan.' in serialized


def test_mixed_tool_call_speech_survives_beyond_the_recent_fallback_tail():
    compressor = _compressor()
    turns = _turns('We chose the violet plan.', 'dict')
    turns[0]['tool_calls'].append({'id': 'work', 'type': 'function', 'function': {
        'name': 'terminal', 'arguments': json.dumps({'command': 'echo done'})}})
    turns.append({'role': 'tool', 'tool_call_id': 'work', 'content': 'done'})
    turns.extend({'role': 'user', 'content': f'Later note {i}.'} for i in range(10))
    fallback = compressor._build_static_fallback_summary(turns, reason='synthetic unavailable provider')
    assert 'We chose the violet plan.' in fallback
    assert 'delivery-only.wav' not in fallback


@pytest.mark.parametrize('visible,speech', [
    ('Balance: 5', 'Balance: -5'),
    ('Balance: -5', 'Balance: +5'),
    ('Version 1.5', 'Version 1 5'),
    ('Rule x < y', 'Rule x > y'),
    ('Identifier alpha', 'Identifier ALPHA'),
    ('Value 5', 'Value 5!'),
    ('Factorial: n', 'Factorial: n!'),
])
def test_distinct_spoken_values_are_not_treated_as_visible_duplicates(visible, speech):
    compressor = _compressor()
    turns = _turns(speech, 'dict')
    turns[0]['content'] = visible
    serialized = compressor._serialize_for_summary(turns)
    assert '[Spoken assistant output]' in serialized
    assert speech in serialized
    fallback = compressor._build_static_fallback_summary(turns, reason='synthetic unavailable provider')
    assert speech in fallback
