"""Configured auxiliary fallbacks publish one activation per selected lane (#183)."""
import json

import httpx
from openai import OpenAI
import pytest

from agent import auxiliary_client as ac
from hermes_cli.plugins import get_plugin_manager
from hermes_constants import get_hermes_home


@pytest.fixture
def routes(monkeypatch):
    home = get_hermes_home()
    observer = home / 'plugins' / 'probe'
    observer.mkdir(parents=True)
    (observer / 'plugin.yaml').write_text('name: probe\nversion: 1.0.0\n')
    (observer / '__init__.py').write_text(
        "events = []\ndef register(ctx):\n    ctx.register_hook('fallback_activated', lambda **kw: events.append(kw))\n")
    config = {
        'plugins': {'enabled': ['probe']},
        'auxiliary': {'title_generation': {
            'provider': 'custom:primary', 'model': 'primary-model',
            'fallback_chain': [{'provider': 'custom:backup', 'model': 'backup-model'}]}},
        'custom_providers': [
            {'name': name, 'base_url': f'https://{name}.invalid/v1',
             'api_key': 'synthetic-secret', 'model': f'{name}-model'}
            for name in ('primary', 'backup')],
    }
    (home / 'config.yaml').write_text(json.dumps(config))
    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    events = manager._plugins['probe'].module.events
    requests = []

    def send(request):
        requests.append(request.url.host)
        if request.url.host == 'primary.invalid':
            return httpx.Response(402, json={'error': {'message': 'Insufficient credits: synthetic-secret'}})
        return httpx.Response(200, json={
            'id': 'synthetic', 'object': 'chat.completion', 'created': 0,
            'model': 'backup-model', 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': 'OK'}}]})

    clients = []
    def client(*args, **kwargs):
        kwargs.update(http_client=httpx.Client(transport=httpx.MockTransport(send)), max_retries=0)
        result = OpenAI(*args, **kwargs)
        clients.append(result)
        return result
    monkeypatch.setattr(ac, 'OpenAI', client)
    ac._reset_aux_unhealthy_cache()
    yield events, requests, send
    for item in clients:
        item.close()
    ac._reset_aux_unhealthy_cache()


def test_configured_auxiliary_fallback_emits_each_distinct_activation(routes):
    events, requests, _ = routes
    for _ in range(2):
        ac._reset_aux_unhealthy_cache()
        result = ac.call_llm(task='title_generation', messages=[{'role': 'user', 'content': 'title'}])
        assert result.choices[0].message.content == 'OK'
    assert requests == ['primary.invalid', 'backup.invalid'] * 2
    assert [(e['stage'], e['provider'], e['model']) for e in events] == [
        ('auxiliary', 'custom:backup', 'backup-model')] * 2
    assert all(e['profile_name'] == 'default' and e['task'] == 'title_generation' for e in events)
    assert len({e['event_id'] for e in events}) == 2
    assert 'synthetic-secret' not in json.dumps(events)
    assert '.invalid' not in json.dumps(events)


@pytest.mark.asyncio
async def test_async_fallback_uses_same_configured_route_and_activation(routes, monkeypatch):
    from openai import AsyncOpenAI
    events, requests, send = routes
    clients = []
    def to_async(client, model, **kwargs):
        result = AsyncOpenAI(api_key='synthetic-secret', base_url=str(client.base_url),
                             max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(send)))
        clients.append(result)
        return result, model
    monkeypatch.setattr(ac, '_to_async_client', to_async)
    try:
        result = await ac.async_call_llm(task='title_generation', messages=[{'role': 'user', 'content': 'title'}])
        assert result.choices[0].message.content == 'OK'
        assert requests == ['primary.invalid', 'backup.invalid']
        assert [(e['stage'], e['provider'], e['model']) for e in events] == [
            ('auxiliary', 'custom:backup', 'backup-model')]
    finally:
        for client in clients:
            await client.close()


def test_compression_fallback_routes_emit_without_changing_prompt_or_leaking_keys(routes):
    from agent.context_compressor_summary import SummaryDispatchMixin
    events, _, _ = routes
    compressor = SummaryDispatchMixin()
    compressor.provider, compressor.model, compressor.summary_model = 'custom:main', 'main-model', 'summary-model'
    compressor._last_aux_resolved_provider = 'custom:summary'
    compressor._last_aux_resolved_model = 'summary-model'
    compressor._clear_compression_failure_cooldown = lambda: None
    compressor._fallback_to_main_for_compression(Exception('synthetic-secret'), 'failed')
    kwargs = {'task': 'compression'}
    compressor._apply_summary_route(kwargs, {'provider': 'custom:backup', 'model': 'backup-model', 'api_key': 'synthetic-secret'})
    assert [(e['old_provider'], e['old_model'], e['provider'], e['model'], e['reason']) for e in events] == [
        ('custom:summary', 'summary-model', 'custom:main', 'main-model', 'summary_failed'),
        ('custom:summary', 'summary-model', 'custom:backup', 'backup-model', 'summary_stalled')]
    assert 'synthetic-secret' not in json.dumps(events)


def test_missing_explicit_credentials_activates_configured_backup_once(routes):
    events, requests, _ = routes
    home = get_hermes_home()
    config = json.loads((home / 'config.yaml').read_text())
    config['auxiliary']['title_generation']['provider'] = 'nous'
    (home / 'config.yaml').write_text(json.dumps(config))
    result = ac.call_llm(task='title_generation', messages=[{'role': 'user', 'content': 'title'}])
    assert result.choices[0].message.content == 'OK'
    assert requests == ['backup.invalid']
    assert [(e['old_provider'], e['provider'], e['reason']) for e in events] == [
        ('nous', 'custom:backup', 'provider_unavailable')]
