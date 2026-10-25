"""Recover satisfied messaging-goal waits through the existing gateway poller and FIFO."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger("gateway.run")


@dataclass(frozen=True)
class _WaitWake:
    owner: str
    identity: str
    prompt: str
    queued: bool = False


def _waiting_sessions(db):
    from hermes_cli.goals import GoalState

    pending = set()
    for key, raw in db.list_meta_prefix('goal:'):
        try:
            state = GoalState.from_json(raw)
            if state.status == 'active' and state.waiting_wake_id:
                pending.add(key.removeprefix('goal:'))
        except Exception:
            logger.warning('Unreadable goal wait %s; other goals remain eligible', key, exc_info=True)
    return pending


def _ready_waits(runner):
    from gateway.run import _profile_runtime_scope
    from gateway.run_heartbeat_restore import _watched_homes
    from gateway.run_idle_gates import _profile_session_db_probe
    from hermes_cli.goals import GoalManager
    from hermes_constants import get_hermes_home

    store = runner.session_store
    home = getattr(store, '_routing_home', None) or get_hermes_home()
    pending = {}
    for profile_home in _watched_homes(runner, home):
        try:
            db = _profile_session_db_probe(profile_home)
            if db is None:
                continue
            pending[profile_home] = _waiting_sessions(db)
        except Exception:
            logger.warning('Goal wait scan failed for %s; retrying next poll', profile_home, exc_info=True)
    if not any(pending.values()):
        return []
    ready = []
    with _profile_runtime_scope(home):
        grouped = {}
        for entry in store.list_sessions():
            if entry.origin is None or entry.suspended or entry.origin.platform.value in ('local', 'api_server'):
                continue
            try:
                source = runner._restored_source(entry)
                scope = runner._profile_scope_key_for_source(source)
                if entry.session_id in pending.get(scope or home, set()):
                    grouped.setdefault(scope, []).append((entry, source))
            except Exception:
                logger.warning('Goal wait route %s unavailable; retaining readiness', entry.session_key, exc_info=True)
        for group in grouped.values():
            try:
                with runner._profile_scope_for_source(group[0][1]):
                    for entry, source in group:
                        manager = GoalManager(entry.session_id)
                        if (manager.is_active() and manager.state.waiting_wake_id
                                and not manager.is_waiting(clear_satisfied=False)):
                            ready.append((entry.session_key, entry.session_id, source))
            except Exception:
                logger.warning('Goal wait profile for %s unavailable; retrying next poll',
                               group[0][0].session_key, exc_info=True)
    return ready


async def poll_goal_waits(runner):
    """Readiness is durable goal state; admission is an idempotent native FIFO record."""
    from agent.estop import check_paused
    from gateway.session_authority import LiveSession
    from gateway.session_authorities import active_authority
    from gateway.session_automation import admit_route_wake
    from hermes_cli.goals import GoalManager
    from hermes_state_runtime import list_session_admissions

    try:
        ready = await runner._run_in_executor_with_context(_ready_waits, runner)
    except Exception:
        logger.warning('Goal wait recovery scan failed; retrying next poll', exc_info=True)
        return
    for key, session_id, source in ready:
        try:
            async with runner._async_profile_scope_for_source(source):
                if check_paused('goal', logger):
                    continue
                entry = runner.session_store.lookup_by_session_key(key)
                if entry is None or entry.suspended or entry.session_id != session_id:
                    continue
                authority = active_authority(runner)
                adapter = runner._delivery_adapter_for(source)
                if authority is None or adapter is None:
                    logger.debug('Goal wait for %s has no available owner; retaining readiness', key)
                    continue

                def prepare():
                    manager = GoalManager(session_id)
                    if (not manager.is_active() or not manager.state.waiting_wake_id
                            or manager.is_waiting(clear_satisfied=False)):
                        return None
                    base = ['goal-wait', manager.state.created_at, manager.state.waiting_wake_id]
                    prefix = json.dumps(base, separators=(',', ':'))[:-1] + ','
                    owner = authority.logical_owner(session_id)
                    previous = next((row for row in reversed(list_session_admissions(
                        authority.db, session_id=owner, pending_only=False))
                        if row['principal_id'] == 'automation:' + key and row['request_id'].startswith(prefix)), None)
                    if previous is not None and previous['status'] != 'terminal':
                        return _WaitWake(owner, previous['request_id'], '',
                                         queued=previous['status'] in ('queued', 'unknown'))
                    if previous is not None and previous['outcome'] != 'interrupted':
                        return None
                    identity = json.dumps([*base, previous['admission_id'] if previous else None], separators=(',', ':'))
                    prompt = manager.next_continuation_prompt()
                    if previous is not None:
                        prompt = ('The previous goal continuation was interrupted. Check current task/run state '
                                  'and completed effects before continuing the next unfinished authorized step. '
                                  'Do not repeat completed mutations.\n\n' + prompt)
                    return _WaitWake(owner, identity, prompt)

                prepared = await runner._run_in_executor_with_context(prepare)
                if prepared is None:
                    continue
                if prepared.queued:
                    authority.sessions.setdefault(prepared.owner, LiveSession(source, key))
                    authority.wake_after_worker(prepared.owner)
                elif prepared.prompt:
                    await admit_route_wake(runner, adapter, source, prepared.prompt, 'goal-wait',
                                           identity=prepared.identity)
        except Exception:
            logger.warning('Goal wait delivery for %s failed; retaining readiness for retry', key, exc_info=True)


async def goal_wait_is_current(runner, event, session_key):
    """A queued wake cannot revive a paused/replaced goal or cross a stopped conversation."""
    identity = getattr(event, 'message_id', None)
    if not event.internal or not isinstance(identity, str) or not identity.startswith('["goal-wait",'):
        return True

    def check():
        from hermes_cli.goals import GoalManager
        _producer, created_at, wake_id, _previous = json.loads(identity)
        entry = runner.session_store.lookup_by_session_key(session_key)
        if entry is None or entry.suspended:
            return False
        manager = GoalManager(entry.session_id)
        return (manager.is_active() and manager.state.created_at == created_at
                and manager.state.waiting_wake_id == wake_id and not manager.is_waiting(clear_satisfied=False))

    current = await runner._run_in_executor_with_context(check)
    if current:
        from agent.estop import check_paused
        if check_paused('goal', logger):
            from gateway.session_results import execution_result
            captured = execution_result.get()
            if captured is not None:
                captured['result'] = {'final_response': '', 'messages': [], 'interrupted': True}
            return False
    return current
