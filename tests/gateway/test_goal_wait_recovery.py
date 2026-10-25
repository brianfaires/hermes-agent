"""A parked messaging goal must regain its owner turn without another user message (#29/#92)."""
import asyncio
from pathlib import Path
import sys

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from gateway.session_authority import initialize_session_authority
from gateway.session_results import execution_result
from gateway.run_goal_waits import poll_goal_waits
from hermes_cli import goals
from hermes_state_runtime import RuntimeStoreError, list_session_admissions
from plugins.platforms.discord.adapter import DiscordAdapter
from tests.waiting import async_wait_until


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    import hermes_state

    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', home / 'state.db')
    monkeypatch.setattr(goals, 'judge_goal', lambda *args, **kwargs: ('done', 'offline result accepted', False, None, False))
    return home


async def messaging_runner(monkeypatch):
    monkeypatch.setenv('DISCORD_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token='fixture-token', typing_indicator=False))

    async def send(chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id='sent')

    monkeypatch.setattr(adapter, 'send', send)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    key = runner.session_store._generate_session_key(source)
    executed = []

    async def turn(event, source, quick_key, run_generation):
        executed.append(event.text)
        captured = execution_result.get()
        if captured is not None:
            captured['result'] = {'final_response': 'reply', 'messages': []}
        return 'reply'

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    return runner, adapter, authority, source, key, executed


async def start_work(monkeypatch):
    runner, adapter, authority, source, key, executed = await messaging_runner(monkeypatch)
    await adapter.handle_message(MessageEvent(text='approved work', source=source, message_id='start'))
    await async_wait_until(lambda: executed == ['approved work'], timeout=5, what='initial owner turn')
    sid = runner.session_store.peek_session_id(key)
    await settled(authority, sid)
    return runner, adapter, authority, source, key, executed, sid


async def settled(authority, sid):
    await async_wait_until(lambda: not list_session_admissions(authority.db, session_id=sid),
                          timeout=5, what='native FIFO settlement')


def park(sid, *, expired=True):
    manager = goals.GoalManager(sid)
    manager.set('finish approved work')
    manager.wait_for_seconds(600, 'waiting for a dependency')
    if expired:
        manager.state.waiting_until = 1
        goals.save_goal(sid, manager.state)
    return manager


@pytest.mark.asyncio
async def test_expired_goal_wait_recovers_on_gateway_poll_without_user_input(monkeypatch):
    runner, _adapter, authority, _source, _key, executed, sid = await start_work(monkeypatch)
    park(sid)

    from gateway.run_heartbeat_restore import restore_heartbeat_watches
    ticks = 0

    async def one_poll(runner):
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            raise asyncio.CancelledError
        await restore_heartbeat_watches(runner)

    monkeypatch.setattr('hermes_cli.heartbeat.POLL_SECONDS', 0)
    monkeypatch.setattr('gateway.run_heartbeat_restore.restore_heartbeat_watches', one_poll)
    runner._start_heartbeat_poller()
    await asyncio.gather(runner._heartbeat_poll_task, return_exceptions=True)
    rows = list_session_admissions(authority.db, session_id=sid, pending_only=False)
    assert len(rows) == 2, 'the expired wait must durably admit its next owner turn'
    await async_wait_until(lambda: len(executed) == 2, timeout=5, what='resumed owner turn')
    assert 'finish approved work' in executed[1]
    await settled(authority, sid)
    await poll_goal_waits(runner)
    assert len(list_session_admissions(authority.db, session_id=sid, pending_only=False)) == 2
    assert len(executed) == 2


@pytest.mark.asyncio
async def test_refused_wait_survives_status_read_and_gateway_restart(monkeypatch):
    runner, _adapter, authority, _source, key, _executed, sid = await start_work(monkeypatch)
    park(sid)
    # Other legitimate readers may already have cleared the satisfied barrier.
    assert not goals.GoalManager(sid).is_waiting()
    original = authority.admit_automation

    async def refuse(*args):
        raise RuntimeStoreError('runtime_draining')

    monkeypatch.setattr(authority, 'admit_automation', refuse)
    await poll_goal_waits(runner)
    assert len(list_session_admissions(authority.db, session_id=sid, pending_only=False)) == 1
    monkeypatch.setattr(authority, 'admit_automation', original)
    runner, _adapter, authority, _source, key, resumed = await messaging_runner(monkeypatch)
    assert runner.session_store.peek_session_id(key) == sid
    await poll_goal_waits(runner)
    await settled(authority, sid)
    await poll_goal_waits(runner)
    assert len(resumed) == 1 and 'finish approved work' in resumed[0]
    assert goals.GoalManager(sid).state.turns_used == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['pause', 'replace', 'stop'])
async def test_queued_goal_wait_cannot_override_newer_user_control(monkeypatch, boundary):
    runner, _adapter, authority, _source, key, executed, sid = await start_work(monkeypatch)
    manager = park(sid)
    live = authority.sessions[authority.logical_owner(sid)]
    async with live.claim_gate:
        await poll_goal_waits(runner)
        if boundary == 'pause':
            manager.pause()
        elif boundary == 'replace':
            manager.set('a different authorized request')
        else:
            runner.session_store.suspend_session(key)
    if boundary != 'stop':
        await settled(authority, sid)
    else:
        # Canonical preclaim retains an input whose conversation was suspended.
        await async_wait_until(lambda: live.task is None or live.task.done(), timeout=5, what='stopped drain')
    assert executed == ['approved work']


@pytest.mark.asyncio
async def test_global_pause_defers_goal_readiness_until_resume(monkeypatch):
    from agent.estop import engage, disengage

    runner, _adapter, authority, _source, _key, executed, sid = await start_work(monkeypatch)
    park(sid)
    engage('operator pause')
    await poll_goal_waits(runner)
    assert len(list_session_admissions(authority.db, session_id=sid, pending_only=False)) == 1
    disengage()
    await poll_goal_waits(runner)
    await settled(authority, sid)
    assert len(executed) == 2


@pytest.mark.asyncio
async def test_queued_wait_retries_after_preclaim_refusal(monkeypatch):
    from gateway.session_envelope import check_native_route

    runner, _adapter, authority, _source, _key, executed, sid = await start_work(monkeypatch)
    park(sid)

    async def refuse(*args):
        raise RuntimeStoreError('permission_denied')

    monkeypatch.setattr('gateway.session_envelope.check_native_route', refuse)
    await poll_goal_waits(runner)
    live = authority.sessions[authority.logical_owner(sid)]
    await async_wait_until(lambda: live.task.done(), timeout=5, what='refused preclaim')
    assert [row['status'] for row in list_session_admissions(authority.db, session_id=sid)] == ['queued']
    monkeypatch.setattr('gateway.session_envelope.check_native_route', check_native_route)
    await poll_goal_waits(runner)
    await settled(authority, sid)
    assert len(executed) == 2
    assert len(list_session_admissions(authority.db, session_id=sid, pending_only=False)) == 2


@pytest.mark.asyncio
async def test_global_pause_after_admission_keeps_the_goal_due_for_resume(monkeypatch):
    from agent.estop import engage, disengage

    runner, _adapter, authority, _source, _key, executed, sid = await start_work(monkeypatch)
    park(sid)
    live = authority.sessions[authority.logical_owner(sid)]
    async with live.claim_gate:
        await poll_goal_waits(runner)
        engage('pause after queueing')
    await settled(authority, sid)
    assert executed == ['approved work']
    disengage()
    await poll_goal_waits(runner)
    await settled(authority, sid)
    assert len(executed) == 2


@pytest.mark.asyncio
async def test_interrupted_owner_is_reconciled_after_gateway_restart(monkeypatch):
    runner, _adapter, authority, _source, _key, _executed, sid = await start_work(monkeypatch)
    park(sid)
    started = asyncio.Event()

    async def interrupted_owner(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, '_handle_message_with_agent', interrupted_owner)
    await poll_goal_waits(runner)
    await asyncio.wait_for(started.wait(), 5)
    task = authority.sessions[authority.logical_owner(sid)].task
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    runner, _adapter, authority, _source, _key, resumed = await messaging_runner(monkeypatch)
    rows = list_session_admissions(authority.db, session_id=sid, pending_only=False)
    assert rows[-1]['outcome'] == 'interrupted'
    await poll_goal_waits(runner)
    await settled(authority, sid)
    await poll_goal_waits(runner)
    assert len(resumed) == 1 and 'completed effects' in resumed[0]
    rows = list_session_admissions(authority.db, session_id=sid, pending_only=False)
    assert [row['outcome'] for row in rows] == ['completed', 'interrupted', 'completed']


@pytest.mark.asyncio
@pytest.mark.platforms('linux')
async def test_live_worker_wait_is_quiet_then_exit_wakes_once(monkeypatch):
    runner, _adapter, authority, _source, _key, executed, sid = await start_work(monkeypatch)
    manager = goals.GoalManager(sid)
    manager.set('finish after the worker exits')
    worker = await asyncio.create_subprocess_exec(
        sys.executable, '-c', 'import sys; sys.stdin.buffer.read()', stdin=asyncio.subprocess.PIPE)
    try:
        manager.wait_on(worker.pid, 'worker running')
        await poll_goal_waits(runner)
        await poll_goal_waits(runner)
        assert len(list_session_admissions(authority.db, session_id=sid, pending_only=False)) == 1
        assert goals.GoalManager(sid).state.turns_used == 0
        await asyncio.wait_for(worker.communicate(), 5)
        await poll_goal_waits(runner)
        await settled(authority, sid)
        await poll_goal_waits(runner)
        assert len(executed) == 2
        assert goals.GoalManager(sid).state.turns_used == 1
    finally:
        if worker.returncode is None:
            worker.kill()
        await asyncio.wait_for(worker.wait(), 5)


@pytest.mark.asyncio
async def test_restore_reads_the_owning_profiles_wait_not_the_launch_profiles(isolated_home, monkeypatch):
    from gateway.run import _profile_runtime_scope
    from gateway.run_goal_waits import _ready_waits
    from hermes_state import SessionDB

    home = isolated_home
    named = home / 'profiles' / 'work'
    named.mkdir(parents=True)
    (named / 'config.yaml').write_text('{}')
    dbs = {str(path): SessionDB(db_path=path / 'state.db') for path in (home, named)}
    monkeypatch.setattr(goals, '_DB_CACHE', dbs)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.session_store = SessionStore(home / 'sessions', runner.config)
    runner._profile_name_for_source = lambda source, adapter_profile=None: source.profile
    runner._delivery_adapter_for = lambda source: object()
    runner._run_in_executor_with_context = asyncio.to_thread
    try:
        source = SessionSource(platform=Platform.DISCORD, chat_id='42', user_id='42', profile='work')
        with _profile_runtime_scope(named):
            entry = runner.session_store.get_or_create_session(source)
            park(entry.session_id)
        with _profile_runtime_scope(home):
            goals.GoalManager(entry.session_id).set('unrelated launch-profile goal')
        # The poller may itself have inherited work's scope, as on a command-triggered start.
        with _profile_runtime_scope(named):
            ready = await runner._run_in_executor_with_context(_ready_waits, runner)
        assert [(key, sid, origin.profile) for key, sid, origin in ready] == [
            (entry.session_key, entry.session_id, 'work')]
        assert goals.GoalManager(entry.session_id).state.goal == 'unrelated launch-profile goal'
    finally:
        runner.session_store.close_all_db_handles()
        for db in dbs.values():
            db.close()
