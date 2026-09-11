"""Nonvoice G17 transport/runtime separation with real temporary pairing stores."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agent import secret_scope
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.pairing import PairingStore
from gateway.profile_routing import ProfileRouteRejected
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import SessionSource
from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
from plugins.platforms.discord import adapter as discord_adapter


@contextlib.contextmanager
def home_scope(home):
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


class TransportIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / '.hermes'
        self.a, self.b = (self.root / 'profiles' / name for name in ('a', 'b'))
        for home in (self.root, self.a, self.b):
            home.mkdir(parents=True, exist_ok=True)
        p = patch.dict(os.environ, {'HOME': self.tmp.name, 'HERMES_HOME': str(self.root)})
        p.start(); self.addCleanup(p.stop)
        p = patch.object(secret_scope, '_MULTIPLEX_ACTIVE', True)
        p.start(); self.addCleanup(p.stop)
        token = secret_scope.set_secret_scope({})
        self.addCleanup(secret_scope.reset_secret_scope, token)
        self.runner = object.__new__(GatewayRunner)
        self.runner.config = GatewayConfig(multiplex_profiles=True)
        self.runner._launch_profile_home = self.root
        self.runner._launch_profile_name = 'default'
        self.runner.adapters = {}
        self.runner._profile_adapters = {}
        self.runner.pairing_stores = {}
        with home_scope(self.root):
            self.runner.pairing_store = PairingStore()

    def make_adapter(self, home, owner=None):
        with home_scope(home):
            obj = discord_adapter.DiscordAdapter(PlatformConfig(enabled=True, token='fixture'))
        obj.set_owner_profile(owner)
        obj._running = True
        obj.gateway_runner = self.runner
        return obj

    def approve_fixture(self, home, user):
        with home_scope(home):
            store = PairingStore()
            store._approved_path('discord').write_text(json.dumps({user: {'user_name': 'fixture'}}))
            return store

    def test_launch_home_and_signature_do_not_follow_ambient_profile(self):
        source = SessionSource(platform=Platform.DISCORD, chat_id='1')
        with home_scope(self.b):
            self.assertEqual(self.runner._active_profile_name(), 'default')
            self.assertEqual(self.runner._resolve_profile_home_for_source(source), self.root)
        signature = GatewayRunner._agent_config_signature
        kwargs = dict(model='fixture', runtime={}, enabled_toolsets=[], ephemeral_prompt='frozen')
        self.assertEqual(signature(**kwargs, profile_home=str(self.a)), signature(**kwargs, profile_home=str(self.a)))
        self.assertNotEqual(signature(**kwargs, profile_home=str(self.a)), signature(**kwargs, profile_home=str(self.b)))

    async def test_nonmultiplex_agent_wrapper_pins_home_and_restores_on_failure(self):
        self.runner.config.multiplex_profiles = False
        seen = []
        async def inner(*args, **kwargs):
            seen.append(get_hermes_home())
            raise RuntimeError('fixture')
        self.runner._run_agent_inner = inner
        source = SessionSource(platform=Platform.DISCORD, chat_id='1')
        with home_scope(self.b):
            with self.assertRaises(RuntimeError):
                await self.runner._run_agent('hello', '', [], source, 'session')
            self.assertEqual(get_hermes_home(), self.b)
        self.assertEqual(seen, [self.root])

    def test_explicit_missing_profile_is_rejected_without_home_fallback(self):
        source = SessionSource(platform=Platform.DISCORD, chat_id='1', profile='missing')
        with home_scope(self.b), self.assertRaises(ProfileRouteRejected):
            self.runner._resolve_profile_home_for_source(source)

    def test_scope_setup_exception_restores_home_and_secrets(self):
        with home_scope(self.a):
            original = secret_scope.current_secret_scope()
            with patch('agent.secret_scope.refresh_profile_secret_scope', side_effect=RuntimeError('fixture')):
                with self.assertRaises(RuntimeError), _profile_runtime_scope(self.b):
                    self.fail('scope entered despite failed refresh')
            self.assertEqual(get_hermes_home(), self.a)
            self.assertIs(secret_scope.current_secret_scope(), original)

    def test_adapter_storage_stays_on_construction_home_after_routing(self):
        obj = self.make_adapter(self.a, 'a')
        with home_scope(self.b):
            obj._nonconversational_messages.mark_many(['123'])
            path = obj._command_sync_state_path()
            path.write_text('{"fixture": true}')
            self.assertTrue(path.is_relative_to(self.a))
            self.assertTrue(obj._nonconversational_messages._state_path().is_relative_to(self.a))
        self.assertFalse((self.b / 'discord').exists())

    def test_source_owner_is_stamped_before_session_key_and_route_still_wins(self):
        obj = self.make_adapter(self.a, 'a')
        source = obj.build_source(chat_id='1', chat_type='dm', user_id='7')
        self.assertEqual(source.profile, 'a')
        self.assertEqual(obj._session_key_profile(source), 'a')
        with patch.object(self.runner, '_profile_name_for_source', return_value='b'):
            routed = obj.build_source(chat_id='1', chat_type='dm', user_id='7')
        self.assertEqual(routed.profile, 'b')
        self.assertNotIn('_transport_adapter_ref', routed.to_dict())

    def test_transport_pairing_ignores_routed_runtime_and_missing_store_fails_closed(self):
        obj = self.make_adapter(self.a, 'a')
        self.runner._profile_adapters = {'a': {Platform.DISCORD: obj}}
        self.runner.pairing_stores['a'] = self.approve_fixture(self.a, '7')
        self.runner.pairing_stores['b'] = self.approve_fixture(self.b, '8')
        source = obj.build_source(chat_id='1', user_id='7')
        source.profile = 'b'
        self.assertIs(self.runner._pairing_store_for(source), self.runner.pairing_stores['a'])
        self.assertTrue(self.runner._pairing_store_for(source).is_approved('discord', '7'))
        self.assertFalse(self.runner._pairing_store_for(source).is_approved('discord', '8'))
        del self.runner.pairing_stores['a']
        self.assertIsNone(self.runner._pairing_store_for(source))

    def test_real_authorization_uses_transport_grant_not_routed_allow_all(self):
        obj = self.make_adapter(self.a, 'a')
        self.runner._profile_adapters = {'a': {Platform.DISCORD: obj}}
        self.runner.pairing_stores['a'] = self.approve_fixture(self.a, '7')
        (self.b / '.env').write_text('DISCORD_ALLOW_ALL_USERS=true\n')
        source = obj.build_source(chat_id='1', chat_type='dm', user_id='7'); source.profile = 'b'
        with _profile_runtime_scope(self.b):
            self.assertTrue(self.runner._is_user_authorized_for_source(source))
            source.user_id = '8'
            self.assertFalse(self.runner._is_user_authorized_for_source(source))

    def test_primary_transport_pairing_stays_on_launch_store(self):
        obj = self.make_adapter(self.root)
        self.runner.adapters = {Platform.DISCORD: obj}
        source = obj.build_source(chat_id='1'); source.profile = 'b'
        self.runner.pairing_stores['b'] = self.approve_fixture(self.b, '8')
        self.assertIs(self.runner._pairing_store_for(source), self.runner.pairing_store)

    def test_stopped_or_unregistered_transport_never_falls_through_to_routed_bot(self):
        a, b = self.make_adapter(self.a, 'a'), self.make_adapter(self.b, 'b')
        self.runner._profile_adapters = {'a': {Platform.DISCORD: a}, 'b': {Platform.DISCORD: b}}
        source = a.build_source(chat_id='1'); source.profile = 'b'
        self.assertIs(self.runner._adapter_for_source(source), a)
        a._running = False
        self.assertIsNone(self.runner._adapter_for_source(source))
        del self.runner._profile_adapters['a']
        self.assertIsNone(self.runner._adapter_for_source(source))
        self.assertIsNone(self.runner._pairing_store_for(source))
        b._running = False
        restored = SessionSource(platform=Platform.DISCORD, chat_id='1', profile='b')
        self.assertIsNone(self.runner._adapter_for_source(restored))

    async def test_secondary_handler_routes_runtime_but_pins_transport_authorization(self):
        source = SessionSource(platform=Platform.DISCORD, chat_id='1', profile='b')
        seen = []
        async def handle(event):
            seen.append((get_hermes_home(), event.source._authorization_profile_home))
        self.runner._handle_message = handle
        await self.runner._make_profile_message_handler('a')(SimpleNamespace(source=source))
        self.assertEqual(seen, [(self.b, self.a)])

    def test_unauthorized_dm_policy_and_pairing_are_transport_scoped(self):
        obj = self.make_adapter(self.a, 'a')
        self.runner._profile_adapters = {'a': {Platform.DISCORD: obj}}
        (self.a / '.env').write_text('DISCORD_ALLOWED_USERS=transport-owner\n')
        source = obj.build_source(chat_id='1', chat_type='dm', user_id='7'); source.profile = 'b'
        with _profile_runtime_scope(self.b):
            self.assertEqual(self.runner._unauthorized_dm_behavior_for_source(source), 'ignore')
            self.assertEqual(get_hermes_home(), self.b)

    def test_adapter_pairing_reads_own_store_under_foreign_runtime(self):
        self.approve_fixture(self.a, '7'); self.approve_fixture(self.b, '8')
        obj = self.make_adapter(self.a, 'a')
        with home_scope(self.b):
            self.assertTrue(obj._is_pairing_approved_user('7'))
            self.assertFalse(obj._is_pairing_approved_user('8'))
            self.assertEqual(get_hermes_home(), self.b)

    async def test_actual_approval_send_captures_transport_for_delayed_view(self):
        self.approve_fixture(self.a, '7'); self.approve_fixture(self.b, '8')
        obj = self.make_adapter(self.a, 'a')
        obj._gate_env_snapshot = {}
        channel = SimpleNamespace(id=123, name='fixture', parent_id=None,
            send=AsyncMock(return_value=SimpleNamespace(id=1)))
        obj._client = SimpleNamespace(get_channel=lambda _: channel)
        with home_scope(self.b):
            result = await obj.send_exec_approval('123', 'echo fixture', 'fixture')
            self.assertTrue(result.success, result.error)
            view = channel.send.call_args.kwargs['view']
            try:
                self.assertEqual(view._pairing_home, self.a)
                self.assertTrue(view._check_auth(SimpleNamespace(user=SimpleNamespace(id=7))))
                self.assertFalse(view._check_auth(SimpleNamespace(user=SimpleNamespace(id=8))))
            finally:
                view.stop()

    async def test_all_six_control_views_pin_pairing_and_authorization(self):
        self.approve_fixture(self.a, '7'); self.approve_fixture(self.b, '8')
        shared = dict(allowed_user_ids=set(), pairing_home=self.a, auth_env={})
        views = [
            discord_adapter.ExecApprovalView(session_key='fixture', **shared),
            discord_adapter.SlashConfirmView(session_key='fixture', confirm_id='c', **shared),
            discord_adapter.UpdatePromptView(session_key='fixture', **shared),
            discord_adapter.ModelPickerView(providers=[], current_model='', current_provider='', session_key='fixture', on_model_selected=lambda *a: None, **shared),
            discord_adapter.ChoicePickerView(choices=[], on_choice_selected=lambda *a: None, **shared),
            discord_adapter.ClarifyChoiceView(choices=[], clarify_id='c', **shared),
        ]
        with home_scope(self.b):
            token = secret_scope.set_secret_scope({'DISCORD_ALLOW_ALL_USERS': 'true'})
            try:
                for view in views:
                    with self.subTest(view=type(view).__name__):
                        self.assertTrue(view._check_auth(SimpleNamespace(user=SimpleNamespace(id=7))))
                        self.assertFalse(view._check_auth(SimpleNamespace(user=SimpleNamespace(id=8))))
            finally:
                secret_scope.reset_secret_scope(token)
                for view in views: view.stop()


if __name__ == '__main__':
    unittest.main()
