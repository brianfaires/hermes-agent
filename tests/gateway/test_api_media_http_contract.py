"""HTTP rendering preserves literal examples, structured items and the source transcript."""
import cli
import gateway.run
import run_agent
import tui_gateway.server

import copy
import logging
from unittest.mock import patch

import pytest

from gateway.platforms.api_server_media import resolve_media_to_data_urls
from tests.gateway.test_api_server_reasoning_nonstream import _app


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['chat', 'responses'])
@pytest.mark.parametrize('standalone', [False, True])
async def test_openai_media_rendering_over_http(tmp_path, monkeypatch, route, standalone):
    image = tmp_path / 'image with spaces.png'
    image.write_bytes(b'synthetic image bytes')
    monkeypatch.setattr('gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS', (tmp_path,))
    text = ('Summary.\n' if standalone else 'For example ') + 'MEDIA:' + str(image)
    client, adapter = _app()

    async def agent(**kwargs):
        return {'final_response': text, 'completed': True,
                'messages': [{'role': 'assistant', 'content': text}]}, {}

    endpoint = '/v1/chat/completions' if route == 'chat' else '/v1/responses'
    body = ({'model': 'hermes-agent', 'messages': [{'role': 'user', 'content': 'q'}]}
            if route == 'chat' else {'model': 'hermes-agent', 'input': 'q', 'store': False})
    async with client:
        with patch.object(adapter, '_run_agent', side_effect=agent):
            response = await client.post(endpoint, json=body)
            assert response.status == 200, await response.text()
            payload = await response.json()
    rendered = (payload['choices'][0]['message']['content'] if route == 'chat'
                else next(item for item in payload['output'] if item['type'] == 'message')['content'][0]['text'])
    if standalone:
        assert 'data:image/png;base64,' in rendered
        assert 'MEDIA:' not in rendered
    else:
        assert rendered == text


@pytest.mark.asyncio
async def test_responses_rendering_preserves_tool_reasoning_and_source_messages(tmp_path, monkeypatch):
    image = tmp_path / 'image.png'
    image.write_bytes(b'synthetic image bytes')
    monkeypatch.setattr('gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS', (tmp_path,))
    text = f'Summary.\nMEDIA:{image}'
    result = {'final_response': text, 'completed': True, 'messages': [
        {'role': 'assistant', 'content': '', 'reasoning': 'planning', 'tool_calls': [
            {'id': 'call1', 'type': 'function', 'function': {'name': 'fixture', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'call1', 'content': 'tool result'},
        {'role': 'assistant', 'content': text}]}
    before = copy.deepcopy(result)
    client, adapter = _app()

    async def agent(**kwargs):
        return result, {}

    async with client:
        with patch.object(adapter, '_run_agent', side_effect=agent):
            response = await client.post('/v1/responses', json={
                'model': 'hermes-agent', 'input': 'q', 'store': False})
            assert response.status == 200, await response.text()
            payload = await response.json()
    items = payload['output']
    assert [item['type'] for item in items] == ['reasoning', 'function_call', 'function_call_output', 'message']
    assert items[1]['name'] == 'fixture' and items[1]['call_id'] == 'call1'
    assert items[2]['output'] == 'tool result'
    assert 'data:image/png;base64,' in items[3]['content'][0]['text']
    assert result == before


def test_render_failure_keeps_response_readable_and_logs_diagnostic_trace(monkeypatch, caplog):
    text = 'MEDIA:/synthetic/image.png'
    def fail_parser(text):
        raise ValueError('synthetic parser failure')

    monkeypatch.setattr('gateway.media_directives.masked_scan', fail_parser)
    with caplog.at_level(logging.DEBUG, logger='gateway.platforms.api_server_media'):
        assert resolve_media_to_data_urls(text) == text
    assert any(record.exc_info and isinstance(record.exc_info[1], ValueError) for record in caplog.records)
