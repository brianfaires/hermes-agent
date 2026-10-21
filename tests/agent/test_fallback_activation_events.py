"""Actual fallback binding emits facts; refused/exhausted candidates never do."""
from unittest.mock import patch

from hermes_cli.plugins import get_plugin_manager
from tests.agent.test_fallback_exhaustion_cooldown import _make_agent, _mock_client


def _observe():
    manager = get_plugin_manager()
    manager._discovered = True
    events = []
    manager._hooks['fallback_activated'] = [lambda **payload: events.append(payload)]
    return events


def test_bound_fallback_emits_once_and_exhaustion_does_not_emit():
    agent = _make_agent([{'provider': 'openai', 'model': 'gpt-4o'}])
    events = _observe()
    with patch('agent.auxiliary_client.resolve_provider_client',
               return_value=(_mock_client(), 'gpt-4o')):
        assert agent._try_activate_fallback()
        assert not agent._try_activate_fallback()
    assert len(events) == 1
    fact = events[0]
    assert fact['provider'] == agent.provider == 'openai'
    assert fact['model'] == agent.model == 'gpt-4o'
    assert fact['stage'] == 'turn'
    assert not {'api_key', 'base_url', 'messages', 'error', 'response'} & fact.keys()


def test_unusable_fallback_candidate_never_emits_an_activation():
    agent = _make_agent([{'provider': 'openai', 'model': 'gpt-4o'}])
    events = _observe()
    with patch('agent.auxiliary_client.resolve_provider_client', return_value=(None, None)):
        assert not agent._try_activate_fallback()
    assert events == []
