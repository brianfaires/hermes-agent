"""Real config and Discord channel policy paths, with transport effects faked."""
import contextlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agent import secret_scope
from gateway.config import Platform, PlatformConfig, load_gateway_config
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from plugins.platforms.discord.adapter import DiscordAdapter, _standalone_outbound_policy


@contextlib.contextmanager
def scoped(secrets):
    token = secret_scope.set_secret_scope(secrets)
    try:
        with patch.object(secret_scope, '_MULTIPLEX_ACTIVE', True):
            yield
    finally:
        secret_scope.reset_secret_scope(token)


def adapter(extra):
    return DiscordAdapter(PlatformConfig(enabled=True, token='fixture', extra=extra))


class ChannelPrecedenceTests(unittest.IsolatedAsyncioTestCase):
    def test_loader_plugin_bridges_channel_policies_for_all_config_layouts(self):
        import yaml
        for policy in ({'allowed_channels': ['123'], 'ignored_channels': ['456']},
                       {'allowed_channels': [], 'ignored_channels': []}):
            for layout in ({'discord': policy}, {'platforms': {'discord': policy}},
                           {'gateway': {'platforms': {'discord': policy}}}):
                with self.subTest(layout=layout), tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp)
                    (home / 'config.yaml').write_text(yaml.safe_dump(layout))
                    token = set_hermes_home_override(home)
                    try:
                        with patch.dict(os.environ, {'DISCORD_ALLOWED_CHANNELS': 'other',
                            'DISCORD_IGNORED_CHANNELS': 'other'}), scoped({}):
                            before = {k: os.environ.get(k) for k in ('DISCORD_ALLOWED_CHANNELS', 'DISCORD_IGNORED_CHANNELS')}
                            config = load_gateway_config()
                            selected = config.platforms[Platform.DISCORD]
                            obj = adapter(selected.extra)
                            self.assertEqual(obj._get_allowed_channels(), set(policy['allowed_channels']))
                            self.assertEqual(obj._get_ignored_channels(), set(policy['ignored_channels']))
                            self.assertEqual({k: os.environ.get(k) for k in before}, before)
                    finally:
                        reset_hermes_home_override(token)

    async def test_explicit_policy_beats_preconnect_environment_before_send(self):
        channel = SimpleNamespace(id=123, name='fixture', parent_id=None, send=AsyncMock())
        obj = adapter({'allowed_channels': ['456']})
        obj._client = SimpleNamespace(get_channel=lambda _: channel, fetch_channel=AsyncMock(return_value=channel))
        with patch.dict(os.environ, {'DISCORD_ALLOWED_CHANNELS': '123'}):
            result = await obj.send('123', 'fixture')
        self.assertFalse(result.success)
        channel.send.assert_not_awaited()

    def test_empty_explicit_channel_policy_is_not_replaced_by_environment(self):
        for empty in ([], ''):
            with self.subTest(empty=empty), patch.dict(os.environ, {
                'DISCORD_ALLOWED_CHANNELS': '123', 'DISCORD_IGNORED_CHANNELS': '*'}):
                obj = adapter({'allowed_channels': empty, 'ignored_channels': empty})
                self.assertEqual(obj._get_allowed_channels(), set())
                self.assertEqual(obj._get_ignored_channels(), set())

    async def test_connected_snapshot_and_deny_still_win(self):
        channel = SimpleNamespace(id=123, name='fixture', parent_id=None, send=AsyncMock())
        obj = adapter({'allowed_channels': ['123'], 'ignored_channels': []})
        obj._client = SimpleNamespace(get_channel=lambda _: channel, fetch_channel=AsyncMock(return_value=channel))
        obj._gate_env_snapshot = {'DISCORD_ALLOWED_CHANNELS': '*', 'DISCORD_IGNORED_CHANNELS': '123'}
        with patch.dict(os.environ, {'DISCORD_ALLOWED_CHANNELS': '123', 'DISCORD_IGNORED_CHANNELS': ''}):
            result = await obj.send('123', 'fixture')
        self.assertFalse(result.success)
        channel.send.assert_not_awaited()

    def test_scoped_miss_never_borrows_another_profiles_environment(self):
        with patch.dict(os.environ, {'DISCORD_ALLOWED_CHANNELS': 'other', 'DISCORD_IGNORED_CHANNELS': '*'}), scoped({}):
            obj = adapter({})
            obj._snapshot_gate_env()
        self.assertEqual(obj._get_allowed_channels(), set())
        self.assertEqual(obj._get_ignored_channels(), set())

    async def test_standalone_policy_honors_explicit_empty_without_network(self):
        config = PlatformConfig(enabled=True, token='fixture', extra={
            'allowed_channels': [], 'ignored_channels': []})
        with patch.dict(os.environ, {'DISCORD_ALLOWED_CHANNELS': 'other'}), \
             patch('aiohttp.ClientSession', side_effect=AssertionError('no lookup required')) as network:
            allowed, reason = await _standalone_outbound_policy(config, '123', {}, {}, {})
        self.assertTrue(allowed, reason)
        network.assert_not_called()

    def test_other_gate_precedence_unchanged(self):
        with patch.dict(os.environ, {'DISCORD_ALLOWED_USERS': 'env-user'}):
            self.assertEqual(adapter({'allow_from': ['yaml-user']})._gate_raw('allow_from', 'DISCORD_ALLOWED_USERS'), 'env-user')


if __name__ == '__main__':
    unittest.main()
