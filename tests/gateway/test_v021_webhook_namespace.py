"""Real authenticated webhook handler state stays profile/route qualified."""
import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter


class WebhookNamespaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_reload_and_route_delivery_ids_do_not_collide(self):
        route = {'profile': 'a', 'secret': 'fixture', 'prompt': '{value}',
                 'deliver': 'log', 'toolsets': ['terminal']}
        adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
            'routes': {'events': route, 'other': dict(route)}, 'rate_limit': 3}))
        adapter.gateway_runner = SimpleNamespace(config=SimpleNamespace(
            multiplex_profiles=True, multiplex_profile_allowlist=None),
            _profile_name_for_source=lambda source: None)
        captured = []
        async def capture(event):
            captured.append(event)
        adapter.handle_message = capture
        async def post(profile, name='events', delivery='same', valid=True):
            body = b'{"value":"fixture"}'
            signature = 'sha256=' + hmac.new(b'fixture', body, hashlib.sha256).hexdigest()
            async def read(): return body
            request = SimpleNamespace(match_info={'profile': profile, 'route_name': name},
                headers={'X-GitHub-Delivery': delivery,
                         'X-Hub-Signature-256': signature if valid else 'invalid'},
                content_length=len(body), read=read, method='POST')
            result = await adapter._handle_webhook(request)
            await asyncio.gather(*list(adapter._background_tasks))
            return result
        with patch('hermes_cli.profiles.profiles_to_serve', return_value=[('a', '/unused/a'), ('b', '/unused/b')]):
            self.assertEqual((await post('a', valid=False)).status, 401)
            self.assertFalse(adapter._rate_counts)
            self.assertEqual((await post('a')).status, 202)
            self.assertEqual(json.loads((await post('a')).text)['status'], 'duplicate')
            self.assertEqual((await post('a', delivery='next')).status, 202)
            self.assertEqual((await post('a', delivery='blocked')).status, 429)
            # A subscription can be rebound; cached A identity must not block B.
            adapter._routes['events']['profile'] = 'b'
            self.assertEqual((await post('b')).status, 202)
            self.assertEqual((await post('a')).status, 404)
            self.assertEqual((await post('a', name='other')).status, 202)
        self.assertEqual(len(captured), 4)
        keys = [event.source.chat_id for event in captured]
        self.assertEqual(len(set(keys)), 4)
        self.assertEqual(set(adapter._delivery_info), set(keys))
        self.assertEqual([event.message_id for event in captured], ['same', 'next', 'same', 'same'])
        self.assertEqual(captured[2].source.profile, 'b')
        self.assertEqual(adapter.toolsets_for_source(captured[2].source), ['terminal'])
        self.assertEqual(adapter.toolsets_for_source(SimpleNamespace(chat_id='webhook:other:legacy')), ['terminal'])
