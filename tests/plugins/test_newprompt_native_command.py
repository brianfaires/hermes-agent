"""Current newprompt grammar and native Discord input reach the real session authority."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

discord = pytest.importorskip('discord')
if not isinstance(getattr(discord, '__file__', None), str):
    pytest.skip('requires the real discord.py SDK', allow_module_level=True)
from discord.ext import commands

from gateway.platforms.event import MessageEvent
from tests.gateway.test_newprompt_authority import _newprompt_harness
from tests.waiting import async_wait_until


def _interaction():
    channel = MagicMock(spec=discord.DMChannel)
    channel.id = 42
    interaction = MagicMock(spec=discord.Interaction)
    interaction.id, interaction.channel, interaction.channel_id, interaction.guild_id = 456, channel, 42, None
    interaction.user = SimpleNamespace(id=42, name='test', display_name='Test', bot=False)
    interaction.response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())
    interaction.edit_original_response, interaction.delete_original_response = AsyncMock(), AsyncMock()
    return interaction


@pytest.mark.asyncio
@pytest.mark.parametrize('confirm,reload_plugin', [(False, False), (True, False), (False, True)])
async def test_discord_prompt_field_starts_the_confirmed_session(monkeypatch, confirm, reload_plugin, plugin_source=None):
    g = await _newprompt_harness(monkeypatch, confirm=confirm, plugin_source=plugin_source)
    monkeypatch.setenv('DISCORD_COMMAND_SYNC_POLICY', 'bulk')
    bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
    bot.tree.sync = AsyncMock(return_value=[])
    g.adapter._client = bot
    # A live connection snapshots this policy before accepting SDK interactions.
    g.adapter._allowed_user_ids = g.adapter._get_allowed_users()
    g.adapter._register_slash_commands()
    g.adapter._wire_plugin_handlers(bot)
    try:
        command = bot.tree.get_command('new')
        prompt = next((p for p in command.parameters if p.name == 'prompt'), None)
        assert prompt is not None and not prompt.required
        await async_wait_until(lambda: bot.tree.sync.await_count, timeout=5, what='native schema reconciliation')
        if reload_plugin:
            g.manager.discover_and_load(force=True)
            g.adapter.rewire_plugin_handlers()
            await async_wait_until(lambda: bot.tree.sync.await_count >= 2, timeout=5, what='reloaded native schema')
            command = bot.tree.get_command('new')
        await command.callback(_interaction(), prompt='hello')
        if confirm:
            from tools import slash_confirm
            pending = await async_wait_until(lambda: slash_confirm.get_pending(g.key), timeout=5)
            assert g.executed == []
            await slash_confirm.resolve(g.key, pending['confirm_id'], 'once')
        await async_wait_until(lambda: g.executed, timeout=5, what='the native slash prompt')
        new = g.runner.session_store.lookup_by_session_key(g.key).session_id
        assert new != g.old
        assert g.executed == [('hello', new, '42')]
        from hermes_state_runtime import list_session_admissions
        rows = list_session_admissions(g.authority.db, session_id=new, pending_only=False)
        assert len(rows) == 1 and rows[0]['payload']['text'] == 'hello'
        assert rows[0]['payload']['native_text_v1']['source']['user_id'] == '42'
    finally:
        g.manager.clear_gateway_message_injector(g.runner)
        await bot.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,title', [('/new', None), ('/reset Project title', 'Project title')])
async def test_bare_reset_and_explicit_title_do_not_submit_a_prompt(monkeypatch, text, title, plugin_source=None):
    g = await _newprompt_harness(monkeypatch, confirm=False, plugin_source=plugin_source)
    try:
        await g.adapter.handle_message(MessageEvent(text=text, source=g.route, message_id='reset-only'))
        await async_wait_until(lambda: g.runner.session_store.lookup_by_session_key(g.key).session_id != g.old,
                               timeout=5, what='the reset-only session')
        await asyncio.gather(*tuple(g.runner._background_tasks))
        assert g.executed == []
        if title:
            new = g.runner.session_store.lookup_by_session_key(g.key).session_id
            await async_wait_until(lambda: (g.authority.db.get_session(new) or {}).get('title') == title,
                                   timeout=5, what='the explicit session title')
    finally:
        g.manager.clear_gateway_message_injector(g.runner)


@pytest.mark.asyncio
async def test_parenthesized_compatibility_and_cancelled_plain_prompt(monkeypatch, plugin_source=None):
    g = await _newprompt_harness(monkeypatch, confirm=True, plugin_source=plugin_source)
    from tools import slash_confirm
    try:
        await g.adapter.handle_message(MessageEvent(text='/new cancelled text', source=g.route, message_id='cancel'))
        pending = await async_wait_until(lambda: slash_confirm.get_pending(g.key), timeout=5)
        await slash_confirm.resolve(g.key, pending['confirm_id'], 'cancel')
        assert g.runner.session_store.lookup_by_session_key(g.key).session_id == g.old
        assert g.executed == []
        await g.adapter.handle_message(MessageEvent(text='/new (hello)', source=g.route, message_id='compat'))
        pending = await async_wait_until(lambda: slash_confirm.get_pending(g.key), timeout=5)
        await slash_confirm.resolve(g.key, pending['confirm_id'], 'once')
        await async_wait_until(lambda: g.executed, timeout=5, what='the compatibility prompt')
        new = g.runner.session_store.lookup_by_session_key(g.key).session_id
        assert g.executed == [('hello', new, '42')]
    finally:
        g.manager.clear_gateway_message_injector(g.runner)
