"""Native operator preparation/apply and Telegram-only approval adapters.

Local owner authority is not proof of human presence. Enrollment finish explicitly
records an attended provisioner's external verification of the native confirmation.
No RPC verb accepts approval, user IDs, receipt objects, or a caller-selected home.
"""
from __future__ import annotations

import asyncio
from functools import partial
import re
import uuid

from gateway.locked_skill_proposals import Proposals, _read, _require, _save
from hermes_state_runtime import RuntimeStoreError

_FIELDS = {
    'prepare': {'skill', 'destination'}, 'freeze': {'identifier', 'pr_url'},
    'show': {'digest'}, 'apply': {'digest'}, 'reconcile': {'digest'}, 'enroll-start': {'skill'},
    'enroll-finish': {'identifier', 'digest', 'attended_confirmation'},
}


def handlers(connection):
    return {'skills.proposal': partial(rpc, connection)}


def _operate(service, operation, params):
    if operation == 'prepare':
        from hermes_cli.locked_skill_proposals import prepare
        prepared = prepare(home=service.home, skill=params['skill'], destination=params['destination'])
        identifier = uuid.uuid4().hex
        service.db._execute_write(lambda conn: _save(conn, 'prepared:' + identifier, prepared))
        return {'identifier': identifier, **prepared}
    if operation == 'freeze':
        with service.db._read_ctx() as conn:
            prepared = _read(conn, 'prepared:' + params['identifier'])
        _require(prepared is not None, 'not_found')
        return service.freeze(prepared, pr_url=params['pr_url'])
    if operation == 'show':
        with service.db._read_ctx() as conn:
            return service._manifest(conn, params['digest'])
    if operation == 'enroll-start':
        enrollment = service.enrollment_start(params['skill'])
        return {'identifier': enrollment['id'], 'expires': enrollment['expires']}
    if operation == 'enroll-finish':
        _require(params['attended_confirmation'] is True, 'unconfirmed')
        return service.enrollment_finish(params['identifier'], params['digest'])
    return {'apply': service.apply, 'reconcile': service.reconcile}[operation](params['digest'])


async def rpc(connection, ref, params):
    authority, actor = connection.authority, connection.actor
    _require(connection.native_owner and 'session:operator' in actor.capabilities)
    _require(actor.profile_id == authority.profile_id, 'profile_mismatch')
    operation = params.get('operation')
    _require(isinstance(operation, str), 'invalid_params')
    fields = _FIELDS.get(operation)
    _require(fields is not None and set(params) == fields | {'operation'}, 'invalid_params')
    _require(all(isinstance(value, str) and value and len(value) <= 4096
                 for key, value in params.items() if key != 'attended_confirmation'), 'invalid_params')
    if 'digest' in params:
        _require(re.fullmatch('[0-9a-f]{64}', params['digest']) is not None, 'invalid_params')
    authority._require_admission_open()
    from gateway.session_runtime_workers import track_mutation
    task = track_mutation(authority, asyncio.to_thread(_operate, Proposals(authority), operation, params))
    try:
        return {'result': await asyncio.shield(task)}
    except (OSError, ValueError) as exc:
        if isinstance(exc, RuntimeStoreError):
            raise
        raise RuntimeStoreError('proposal_refused', detail=str(exc)) from exc


async def handle_native(runner, event, args):
    from hermes_cli.locked_skill_proposals import canonical
    try:
        return canonical(await native(runner, event, args))
    except (OSError, ValueError) as exc:
        reason = exc.reason if isinstance(exc, RuntimeStoreError) else 'invalid_or_drifted_proposal'
        return 'Locked proposal refused: ' + reason


async def native(runner, event, args):
    from gateway.session_ingress_context import capture_provenance
    from gateway.session_authorities import authority_for_home
    provenance = capture_provenance(runner, event)
    _require(provenance is not None)
    authority = authority_for_home(runner, provenance['runtime_home'])
    _require(authority is not None, 'profile_mismatch')
    authority._require_admission_open()
    from gateway.session_runtime_workers import track_mutation
    task = track_mutation(authority, asyncio.to_thread(Proposals(authority).native, runner, event, args))
    return await asyncio.shield(task)
