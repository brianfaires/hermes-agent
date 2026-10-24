"""Actual OpenAI HTTP routes use the shared standalone image-rendering owner."""
import pytest
from unittest.mock import patch

from gateway import run
from tests.gateway.test_api_server_reasoning_nonstream import _app


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['chat', 'responses'])
@pytest.mark.parametrize('standalone', [False, True])
async def test_openai_media_rendering_over_http(tmp_path, monkeypatch, route, standalone):
    file = tmp_path / 'image with spaces.png'
    file.write_bytes(b'synthetic image bytes')
    monkeypatch.setattr('gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS', (tmp_path,))
    text = ('Summary.\n' if standalone else 'For example ') + 'MEDIA:' + str(file)
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
