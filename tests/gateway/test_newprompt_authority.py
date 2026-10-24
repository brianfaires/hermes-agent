"""The frozen newprompt consumer starts its confirmed session through the real FIFO."""
import asyncio
import shutil
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import hermes_yaml as yaml

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session_authority import initialize_session_authority
from gateway.session_results import execution_result
from hermes_constants import get_hermes_home
from hermes_cli.plugins import PluginContext, get_plugin_manager
from plugins.platforms.discord.adapter import DiscordAdapter
from tests.waiting import async_wait_until


async def _newprompt_harness(monkeypatch, *, confirm, role_only=False, shared_thread=False, plugin_source=None):
    home = get_hermes_home()
    source = (Path(plugin_source) if plugin_source is not None else
              Path(__file__).resolve().parents[1] / 'plugins' / 'fixtures' / 'newprompt')
    shutil.copytree(source, home / 'plugins' / 'newprompt', ignore=shutil.ignore_patterns('__pycache__'))
    (home / 'config.yaml').write_text(yaml.safe_dump({
        'plugins': {'enabled': ['newprompt'], 'entries': {'newprompt': {'allow_gateway_injection': True}}},
        'approvals': {'destructive_slash_confirm': confirm},
        'discord': {'dm_role_auth_guild': 99},
    }))
    if role_only:
        for key in ('DISCORD_ALLOWED_USERS', 'DISCORD_ALLOW_ALL_USERS', 'GATEWAY_ALLOWED_USERS', 'GATEWAY_ALLOW_ALL_USERS'):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv('DISCORD_ALLOWED_ROLES', '7')
        if shared_thread:
            monkeypatch.setenv('DISCORD_ALLOWED_USERS', '41')
    else:
        monkeypatch.setenv('DISCORD_ALLOWED_USERS', '41,42')
    profile_auth = ('DISCORD_ALLOWED_ROLES=7\nDISCORD_ALLOWED_USERS=' + ('41' if shared_thread else '')
                    if role_only else 'DISCORD_ALLOWED_USERS=41,42')
    (home / '.env').write_text(profile_auth + '\n')
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture', typing_indicator=False))
    async def send(chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id='sent')
    async def send_confirm(*args, **kwargs):
        return SendResult(success=True, message_id='confirm')
    monkeypatch.setattr(adapter, 'send', send)
    monkeypatch.setattr(adapter, 'send_slash_confirm', send_confirm)
    guild = SimpleNamespace(id=99)
    member = SimpleNamespace(id=42, guild=guild, roles=[SimpleNamespace(id=7)])
    guild.fetch_member = AsyncMock(return_value=member)
    adapter._client = SimpleNamespace(get_guild=lambda _: guild)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    executed = []
    async def turn(event, source, quick_key, run_generation):
        executed.append((event.text, event.metadata.get('gateway_session_id'), source.user_id))
        captured = execution_result.get()
        if captured is not None:
            captured['result'] = {'final_response': 'reply', 'messages': []}
        return 'reply'
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    assert not manager._plugins['newprompt'].error
    runner._gateway_loop = asyncio.get_running_loop()
    runner._running = True
    manager.set_gateway_message_injector(runner, runner._schedule_plugin_message_injection)
    route = adapter.build_source(chat_id='42', chat_type='group' if shared_thread else 'dm',
                                 thread_id='99' if shared_thread else None, user_id='42',
                                 scope_id='99' if shared_thread else None,
                                 guild_id='99' if shared_thread else None,
                                 user_name='resetting actor', role_authorized=role_only)
    key = runner._session_key_for_source(route)
    old_source = replace(route, user_id='41', user_name='original actor') if shared_thread else route
    assert runner._session_key_for_source(old_source) == key
    old = runner.session_store.get_or_create_session(old_source).session_id
    return SimpleNamespace(home=home, runner=runner, adapter=adapter, authority=authority,
                           manager=manager, route=route, key=key, old=old, member=member,
                           guild=guild, executed=executed)


@pytest.mark.asyncio
@pytest.mark.parametrize('role_only,confirm,shared_thread,revoke_role', [
    (False, False, False, False), (False, True, False, False), (True, False, False, False),
    (True, True, False, False), (False, True, True, False), (True, True, True, True),
], ids=['allowlist-immediate', 'allowlist-confirm', 'role-immediate', 'role-confirm', 'shared-thread', 'role-revoked'])
async def test_confirmed_prompt_starts_fresh_session_in_native_authority(monkeypatch, confirm, role_only, shared_thread, revoke_role, plugin_source=None):
    g = await _newprompt_harness(monkeypatch, confirm=confirm, role_only=role_only,
                                 shared_thread=shared_thread, plugin_source=plugin_source)
    try:
        ctx = PluginContext(g.manager._plugins['newprompt'].manifest, g.manager)
        assert ctx.inject_message('unrequested', session_key=g.key) is True
        await asyncio.gather(*tuple(g.runner._background_tasks))
        assert g.executed == []
        await g.adapter.handle_message(MessageEvent(text='/new hello', source=g.route, message_id='reset-1'))
        if confirm:
            from tools import slash_confirm
            pending = await async_wait_until(lambda: slash_confirm.get_pending(g.key), timeout=5)
            assert g.executed == []
            if revoke_role:
                g.member.roles = []
            await slash_confirm.resolve(g.key, pending['confirm_id'], 'once')
        await async_wait_until(lambda: g.runner.session_store.lookup_by_session_key(g.key).session_id != g.old,
                               timeout=5, what='the reset to finish')
        new = g.runner.session_store.lookup_by_session_key(g.key).session_id
        assert new != g.old
        if revoke_role:
            await asyncio.gather(*tuple(g.runner._background_tasks))
            from hermes_state_runtime import list_session_admissions
            assert g.executed == []
            assert not list_session_admissions(g.authority.db, session_id=new, pending_only=False)
            g.guild.fetch_member.assert_awaited()
            return
        await async_wait_until(lambda: g.executed, timeout=5, what='the first prompted turn')
        assert g.executed == [('hello', new, '42')]
        if role_only:
            g.guild.fetch_member.assert_awaited()
        from hermes_state_runtime import list_session_admissions
        rows = list_session_admissions(g.authority.db, session_id=g.authority.logical_owner(new), pending_only=False)
        assert len(rows) == 1 and rows[0]['payload']['text'] == 'hello'
        assert rows[0]['principal_id'].startswith('automation:')
        assert rows[0]['payload']['native_text_v1']['provenance']
        assert rows[0]['payload']['native_text_v1']['source']['user_id'] == '42'
        assert not list_session_admissions(g.authority.db, session_id=g.old, pending_only=False)
    finally:
        g.manager.clear_gateway_message_injector(g.runner)
