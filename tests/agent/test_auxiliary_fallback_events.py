"""Configured auxiliary fallbacks publish one activation per selected lane (#183)."""
import json

import httpx
from openai import OpenAI
import pytest

from agent import auxiliary_client as ac
from hermes_cli.plugins import get_plugin_manager
from hermes_constants import get_hermes_home
from tests.waiting import wait_until


@pytest.fixture
def routes(monkeypatch):
    home = get_hermes_home()
    observer = home / 'plugins' / 'probe'
    observer.mkdir(parents=True)
    (observer / 'plugin.yaml').write_text('name: probe\nversion: 1.0.0\n')
    (observer / '__init__.py').write_text(
        "events = []\ngate = None\nentered = None\ndef observe(**kw):\n    events.append(kw)\n    if gate is not None:\n        entered.set()\n        gate.wait(15)\ndef register(ctx):\n    ctx.register_hook('fallback_activated', observe)\n")
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
    modes = []

    def completion(request, text, finish='stop'):
        model = json.loads(request.content).get('model')
        payload = {'id': 'synthetic', 'created': 0, 'model': model,
                   'object': 'chat.completion', 'choices': [{'index': 0, 'finish_reason': finish,
                       'message': {'role': 'assistant', 'content': text}}]}
        if json.loads(request.content).get('stream'):
            payload.update(object='chat.completion.chunk', choices=[{'index': 0, 'finish_reason': finish,
                            'delta': {'role': 'assistant', 'content': text}}])
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  text='data: ' + json.dumps(payload) + '\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json=payload)

    def send(request):
        requests.append(request.url.host)
        if request.url.host == 'primary.invalid' and modes == ['partial']:
            return completion(request, 'partial summary', 'length')
        if request.url.host == 'primary.invalid':
            return httpx.Response(402, json={'error': {'message': 'Insufficient credits: synthetic-secret'}})
        return completion(request, 'OK')

    clients = []
    def client(*args, **kwargs):
        kwargs.update(http_client=httpx.Client(transport=httpx.MockTransport(send)), max_retries=0)
        result = OpenAI(*args, **kwargs)
        clients.append(result)
        return result
    monkeypatch.setattr(ac, 'OpenAI', client)
    ac._reset_aux_unhealthy_cache()
    yield events, requests, send, modes
    for item in clients:
        item.close()
    ac._reset_aux_unhealthy_cache()


def test_configured_auxiliary_fallback_emits_each_distinct_activation(routes):
    events, requests, _, _ = routes
    for _ in range(2):
        ac._reset_aux_unhealthy_cache()
        result = ac.call_llm(task='title_generation', messages=[{'role': 'user', 'content': 'title'}])
        assert result.choices[0].message.content == 'OK'
    assert requests == ['primary.invalid', 'backup.invalid'] * 2
    assert wait_until(lambda: len(events) == 2)
    assert [(e['stage'], e['provider'], e['model']) for e in events] == [
        ('auxiliary', 'custom:backup', 'backup-model')] * 2
    assert all(e['profile_name'] == 'default' and e['task'] == 'title_generation' for e in events)
    assert len({e['event_id'] for e in events}) == 2
    assert 'synthetic-secret' not in json.dumps(events)
    assert '.invalid' not in json.dumps(events)


@pytest.mark.asyncio
async def test_async_fallback_uses_same_configured_route_and_activation(routes, monkeypatch):
    from openai import AsyncOpenAI
    events, requests, send, _ = routes
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
        assert wait_until(lambda: len(events) == 1)
        assert [(e['stage'], e['provider'], e['model']) for e in events] == [
            ('auxiliary', 'custom:backup', 'backup-model')]
    finally:
        for client in clients:
            await client.close()


def test_missing_explicit_credentials_activates_configured_backup_once(routes):
    events, requests, _, _ = routes
    home = get_hermes_home()
    config = json.loads((home / 'config.yaml').read_text())
    config['auxiliary']['title_generation']['provider'] = 'nous'
    (home / 'config.yaml').write_text(json.dumps(config))
    result = ac.call_llm(task='title_generation', messages=[{'role': 'user', 'content': 'title'}])
    assert result.choices[0].message.content == 'OK'
    assert requests == ['backup.invalid']
    assert wait_until(lambda: len(events) == 1)
    assert [(e['old_provider'], e['provider'], e['reason']) for e in events] == [
        ('nous', 'custom:backup', 'provider_unavailable')]


@pytest.mark.parametrize('async_mode', [False, True])
@pytest.mark.asyncio
async def test_auto_route_unavailable_main_notifies_even_with_cached_backup(routes, monkeypatch, async_mode):
    from openai import AsyncOpenAI
    events, requests, send, _ = routes
    config_path = get_hermes_home() / 'config.yaml'
    config = json.loads(config_path.read_text())
    config['auxiliary']['title_generation']['provider'] = 'auto'
    config_path.write_text(json.dumps(config))
    clients = []
    def to_async(client, model, **kwargs):
        result = AsyncOpenAI(api_key='synthetic-secret', base_url=str(client.base_url), max_retries=0,
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(send)))
        clients.append(result)
        return result, model
    monkeypatch.setattr(ac, '_to_async_client', to_async)
    try:
        for _ in range(2):
            kwargs = dict(task='title_generation', main_runtime={'provider': 'nous', 'model': 'main-model'},
                          messages=[{'role': 'user', 'content': 'title'}])
            result = await ac.async_call_llm(**kwargs) if async_mode else ac.call_llm(**kwargs)
            assert result.choices[0].message.content == 'OK'
        assert requests == ['backup.invalid'] * 2
        assert wait_until(lambda: len(events) == 2)
        assert [(e['old_provider'], e['provider'], e['reason']) for e in events] == [
            ('nous', 'custom:backup', 'provider_unavailable')] * 2
    finally:
        for client in clients:
            await client.close()


def test_real_summary_failure_switches_to_main_without_mutating_input(routes):
    from copy import deepcopy
    from agent.context_compressor import ContextCompressor
    events, requests, _, modes = routes
    modes.append('partial')
    config_path = get_hermes_home() / 'config.yaml'
    config = json.loads(config_path.read_text())
    config['auxiliary']['compression'] = {'provider': 'custom:primary', 'model': 'summary-model'}
    config_path.write_text(json.dumps(config))
    compressor = ContextCompressor(model='main-model', provider='custom:backup',
        base_url='https://backup.invalid/v1', api_key='synthetic-secret',
        config_context_length=100000, quiet_mode=True)
    messages = [{'role': 'user', 'content': 'remember this'}, {'role': 'assistant', 'content': 'yes'}]
    before = deepcopy(messages)
    assert 'OK' in compressor._generate_summary(messages)
    assert messages == before
    assert requests == ['primary.invalid', 'backup.invalid']
    assert wait_until(lambda: len(events) == 1)
    assert [(e['old_provider'], e['provider'], e['reason']) for e in events] == [
        ('custom:primary', 'custom', 'summary_failed')]


def test_real_stall_recovery_pins_summary_backup_once(routes):
    import threading
    from agent.context_compressor import ContextCompressor
    from agent.conversation_compression import run_compress_context_with_progress_timeout
    events, requests, _, _ = routes
    config_path = get_hermes_home() / 'config.yaml'
    config = json.loads(config_path.read_text())
    config['auxiliary']['compression'] = {'provider': 'custom:primary', 'model': 'summary-model',
        'fallback_chain': [{'provider': 'custom:backup', 'model': 'backup-model', 'timeout': 10}]}
    config_path.write_text(json.dumps(config))
    compressor = ContextCompressor(model='main-model', provider='custom:primary',
        base_url='https://primary.invalid/v1', api_key='synthetic-secret',
        config_context_length=100000, quiet_mode=True)
    messages = [{'role': 'user', 'content': 'remember this'}]
    release = threading.Event()
    attempts = []
    def worker(fence):
        attempts.append(fence)
        if len(attempts) == 1:
            release.wait(15)
            return messages, 'stable prompt'
        summary = compressor._generate_summary(messages)
        return [{'role': 'user', 'content': summary}], 'stable prompt'
    try:
        result, prompt = run_compress_context_with_progress_timeout(
            worker=worker, messages=messages, system_prompt_fallback='stable prompt',
            idle_timeout_seconds=2, total_ceiling_seconds=12)
    finally:
        release.set()
    assert len(result) == 1 and 'OK' in result[0]['content'] and prompt == 'stable prompt'
    assert messages == [{'role': 'user', 'content': 'remember this'}]
    assert requests == ['backup.invalid'] and len(attempts) == 2
    assert wait_until(lambda: len(events) == 1)
    assert [(e['provider'], e['model'], e['reason']) for e in events] == [
        ('custom:backup', 'backup-model', 'summary_stalled')]


def test_vision_auto_rescue_reports_selected_aggregator(routes, monkeypatch):
    events, requests, _, _ = routes
    monkeypatch.setenv('OPENROUTER_API_KEY', 'synthetic-secret')
    result = ac.call_llm(task='vision', provider='auto',
        main_runtime={'provider': 'nous', 'model': 'main-model'},
        messages=[{'role': 'user', 'content': 'describe'}])
    assert result.choices[0].message.content == 'OK'
    assert requests == ['openrouter.ai']
    assert wait_until(lambda: len(events) == 1)
    assert [(e['old_provider'], e['provider'], e['reason']) for e in events] == [
        ('nous', 'openrouter', 'provider_unavailable')]



def test_unusable_pinned_summary_does_not_report_proposed_route(routes):
    from agent.context_compressor import ContextCompressor, pin_summary_route
    events, requests, _, _ = routes
    config_path = get_hermes_home() / 'config.yaml'
    config = json.loads(config_path.read_text())
    config['auxiliary']['compression'] = {'provider': 'nous', 'model': 'missing', 'fallback_chain': []}
    config_path.write_text(json.dumps(config))
    compressor = ContextCompressor(model='main-model', provider='nous',
                                   config_context_length=100000, quiet_mode=True)
    with pin_summary_route({'provider': 'nous', 'model': 'missing'}):
        assert compressor._generate_summary([{'role': 'user', 'content': 'remember this'}]) is None
    assert requests == [] and events == []


def test_slow_observer_does_not_delay_configured_fallback_request(routes):
    import threading
    from agent.memory_provider import spawn_context_thread
    _events, requests, _, _ = routes
    observer = get_plugin_manager()._plugins['probe'].module
    observer.gate, observer.entered = threading.Event(), threading.Event()
    finished = threading.Event()
    results = []
    def run():
        try:
            results.append(ac.call_llm(task='title_generation', messages=[{'role': 'user', 'content': 'title'}]))
        finally:
            finished.set()
    thread = spawn_context_thread(run, name='test-fallback-caller', daemon=False)
    thread.start()
    try:
        assert observer.entered.wait(5)
        assert finished.wait(5), 'notification observer consumed the inference budget'
        assert results[0].choices[0].message.content == 'OK'
        assert requests == ['primary.invalid', 'backup.invalid']
    finally:
        observer.gate.set()
        thread.join(10)
