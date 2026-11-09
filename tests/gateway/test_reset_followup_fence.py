"""A reset lends its proof to one follow-up, into the session it opened, from its own hook only."""
import asyncio

import pytest

from gateway.platforms.event import MessageEvent
from hermes_cli.plugins import PluginContext
from hermes_state_runtime import list_session_admissions
from tests.gateway.test_newprompt_authority import _newprompt_harness
from tests.waiting import async_wait_until


async def _reset(g, text, message_id):
    before = g.runner.session_store.lookup_by_session_key(g.key).session_id
    await g.adapter.handle_message(MessageEvent(text=text, source=g.route, message_id=message_id))
    await async_wait_until(lambda: g.runner.session_store.lookup_by_session_key(g.key).session_id != before,
                           timeout=5, what='the reset')
    return g.runner.session_store.lookup_by_session_key(g.key).session_id


async def _settle(g, *tasks):
    # The dispatches scheduled so far (a spawned task schedules its own before it finishes).
    await asyncio.gather(*tasks)
    await asyncio.gather(*tuple(g.runner._background_tasks))


def _admitted(g, session_id):
    return list_session_admissions(g.authority.db, session_id=g.authority.logical_owner(session_id),
                                   pending_only=False)


def _ctx(g):
    return PluginContext(g.manager._plugins['newprompt'].manifest, g.manager)


@pytest.mark.asyncio
async def test_second_injection_from_the_same_reset_is_refused(monkeypatch):
    g = await _newprompt_harness(monkeypatch, confirm=False)
    ctx = _ctx(g)
    # Beside newprompt's own follow-up: whichever dispatch reaches the lease first spends it.
    ctx.register_hook('on_session_reset', lambda session_key=None, new_session_id=None, **_:
                      ctx.inject_message('again', session_key=session_key, expected_session_id=new_session_id))
    try:
        new = await _reset(g, '/new hello', 'reset-1')
        await async_wait_until(lambda: g.executed, timeout=5, what='the follow-up')
        await _settle(g)
        assert len(g.executed) == 1 and g.executed[0][1:] == (new, '42')
        assert [row['payload']['text'] for row in _admitted(g, new)] == [g.executed[0][0]]
    finally:
        g.manager.clear_gateway_message_injector(g.runner)


@pytest.mark.asyncio
async def test_wrong_expected_session_id_is_refused(monkeypatch):
    g = await _newprompt_harness(monkeypatch, confirm=False)
    ctx = _ctx(g)
    ctx.register_hook('on_session_reset', lambda session_key=None, old_session_id=None, **_:
                      ctx.inject_message('stale', session_key=session_key, expected_session_id=old_session_id))
    try:
        new = await _reset(g, '/new', 'reset-1')
        await _settle(g)
        assert g.executed == []
        assert not _admitted(g, new)
    finally:
        g.manager.clear_gateway_message_injector(g.runner)


@pytest.mark.asyncio
async def test_follow_up_after_the_session_rotated_again_is_refused(monkeypatch):
    g = await _newprompt_harness(monkeypatch, confirm=False)
    ctx = _ctx(g)
    release, spawned = asyncio.Event(), []

    async def later(session_key):
        await release.wait()
        current = g.runner.session_store.lookup_by_session_key(session_key).session_id
        ctx.inject_message('late', session_key=session_key, expected_session_id=current)

    def hook(session_key=None, **_):
        if not spawned:  # a task spawned inside the first reset's hook inherits its lease
            spawned.append(asyncio.get_running_loop().create_task(later(session_key)))

    ctx.register_hook('on_session_reset', hook)
    try:
        first = await _reset(g, '/new', 'reset-1')
        # The hook runs after the rotation _reset waits on; spawn under reset-1's lease, not reset-2's.
        await async_wait_until(lambda: spawned, timeout=5, what="the first reset's hook")
        second = await _reset(g, '/new', 'reset-2')
        assert second != first
        release.set()
        await _settle(g, *spawned)
        assert g.executed == []
        assert not _admitted(g, first) and not _admitted(g, second)
    finally:
        g.manager.clear_gateway_message_injector(g.runner)


@pytest.mark.asyncio
async def test_injection_after_the_hook_returned_is_refused(monkeypatch):
    g = await _newprompt_harness(monkeypatch, confirm=False)
    try:
        new = await _reset(g, '/new', 'reset-1')
        assert _ctx(g).inject_message('late', session_key=g.key, expected_session_id=new) is True
        await _settle(g)
        assert g.executed == []
        assert not _admitted(g, new)
    finally:
        g.manager.clear_gateway_message_injector(g.runner)
