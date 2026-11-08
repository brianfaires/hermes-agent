"""Opt-in observers see bound fallback routes without provider calls."""
import json

import cli
import pytest
import tui_gateway.server as tui

from hermes_cli.plugins import get_plugin_manager
from hermes_constants import get_hermes_home
from run_agent import AIAgent


def _observer():
    home = get_hermes_home()
    plugin = home / 'plugins' / 'fallback-probe'
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / 'plugin.yaml').write_text('name: fallback-probe\nversion: 1.0.0\n')
    (plugin / '__init__.py').write_text(
        "events = []\ndef register(ctx):\n    ctx.register_hook('fallback_activated', lambda **kwargs: events.append(kwargs))\n")
    (home / 'config.yaml').write_text(json.dumps({
        'plugins': {'enabled': ['fallback-probe']},
        'fallback_providers': [{'provider': 'custom:backup', 'model': 'backup-model'}],
        'model': {'context_length': 131072},
        'custom_providers': [{'name': 'backup', 'base_url': 'http://127.0.0.1:9/v1',
                              'api_key': 'synthetic', 'model': 'backup-model'}],
    }))
    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    return manager._plugins['fallback-probe'].module.events


def test_init_fallback_emits_only_the_bound_route():
    events = _observer()
    agent = AIAgent(model='primary', provider='openai',
                    fallback_model=[{'provider': 'custom:backup', 'model': 'backup-model',
                                     'base_url': 'http://127.0.0.1:9/v1', 'api_key': 'synthetic'}],
                    enabled_toolsets=[], quiet_mode=True, skip_context_files=True,
                    skip_memory=True, save_trajectories=False, checkpoints_enabled=False,
                    session_id='fallback-session', platform='telegram')
    try:
        assert agent.model == 'backup-model'
        assert [(e['stage'], e['provider'], e['model']) for e in events] == [
            ('agent_init', 'custom:backup', 'backup-model')]
        assert events[0]['old_model'] == 'primary'
        assert events[0]['old_provider'] == 'openai'
        assert events[0]['profile_name'] == 'default'
        assert events[0]['session_id'] == 'fallback-session'
        assert 'synthetic' not in json.dumps(events)
    finally:
        agent.close()


def test_shared_runtime_resolution_emits_after_a_usable_fallback():
    from hermes_cli.runtime_provider import resolve_runtime_with_fallback

    events = _observer()
    runtime, entry = resolve_runtime_with_fallback(
        {'fallback_providers': [{'provider': 'custom:backup', 'model': 'backup-model'}]},
        requested='nous', target_model='primary')
    assert runtime['provider'] == 'custom:backup'
    assert entry['model'] == 'backup-model'
    assert [(e['stage'], e['provider'], e['model']) for e in events] == [
        ('runtime_resolution', 'custom:backup', 'backup-model')]


def test_cli_resolution_reports_the_selected_route():
    from hermes_cli.auth import AuthError

    events = _observer()
    console = object.__new__(cli.HermesCLI)
    console._fallback_model = [{'provider': 'custom:backup', 'model': 'backup-model'}]
    console.requested_provider, console.model = 'nous', 'primary'
    runtime = console._resolve_fallback_runtime(AuthError('synthetic missing credentials', provider='nous'))
    assert runtime is not None
    assert console.model == 'backup-model'
    assert [(e['stage'], e['provider'], e['model']) for e in events] == [
        ('cli_runtime', 'custom:backup', 'backup-model')]


def test_tui_resolution_reports_the_selected_route():
    events = _observer()
    with tui._session_profile_runtime_scope({'profile_home': str(get_hermes_home())}):
        result = tui._resolve_runtime_with_fallback({'requested': 'nous', 'target_model': 'primary'})
    assert result.used_fallback
    assert result.selected_model == 'backup-model'
    assert [(e['stage'], e['provider'], e['model']) for e in events] == [
        ('tui_runtime', 'custom:backup', 'backup-model')]


def test_turn_fallback_ignores_errors_failed_candidates_and_manual_switches():
    from agent.error_classifier import FailoverReason
    from hermes_cli.lifecycle import invoke_hook

    events = _observer()
    agent = AIAgent(model='primary', provider='custom', api_key='synthetic-primary',
                    base_url='http://127.0.0.1:9/v1',
                    fallback_model=[{'provider': 'custom:missing', 'model': 'missing'},
                                    {'provider': 'custom:backup', 'model': 'backup-model'}],
                    enabled_toolsets=[], quiet_mode=True, skip_context_files=True,
                    skip_memory=True, save_trajectories=False, checkpoints_enabled=False,
                    session_id='turn-session', platform='telegram')
    try:
        invoke_hook('api_request_error', provider='custom', model='primary', error={'type': 'synthetic'})
        assert events == []
        assert agent._try_activate_fallback(FailoverReason.server_error)
        assert [(e['stage'], e['provider'], e['model'], e['session_id']) for e in events] == [
            ('agent_turn', 'custom:backup', 'backup-model', 'turn-session')]
        assert not agent._try_activate_fallback(FailoverReason.server_error)
        agent.switch_model('gpt-4.1-mini', 'custom:backup')
        assert len(events) == 1
        assert events[0]['reason'] == 'server_error'
        assert 'synthetic' not in json.dumps(events)
    finally:
        agent.close()


def test_served_profile_owns_each_runtime_activation():
    from gateway.run import _profile_runtime_scope
    from hermes_cli.runtime_provider import resolve_runtime_with_fallback

    root = get_hermes_home()
    results = []
    for name in ('alpha', 'beta', 'alpha'):
        with _profile_runtime_scope(root / 'profiles' / name):
            events = _observer()
            resolve_runtime_with_fallback(
                {'fallback_providers': [{'provider': 'custom:backup', 'model': 'backup-model'}]},
                requested='nous', target_model='primary')
            results.extend((e['profile_name'], e['provider'], e['model']) for e in events)
    assert results == [(name, 'custom:backup', 'backup-model') for name in ('alpha', 'beta', 'alpha')]


def test_fully_refused_construction_does_not_emit_activation():
    from agent.auxiliary_unavailable import ProviderNotConfiguredError

    events = _observer()
    with pytest.raises(ProviderNotConfiguredError):
        AIAgent(model='primary', provider='nous',
                fallback_model=[{'provider': 'custom:missing', 'model': 'missing'}],
                enabled_toolsets=[], quiet_mode=True, skip_context_files=True,
                skip_memory=True, save_trajectories=False, checkpoints_enabled=False)
    assert events == []
