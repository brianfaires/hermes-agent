"""Private connector proof for a plugin follow-up to this confirmed command reset."""
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from copy import copy, deepcopy
from dataclasses import dataclass, replace
import json
from typing import Any

from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


@dataclass(frozen=True)
class _Proof:
    runner: Any
    source: SessionSource
    provenance: dict


@dataclass(frozen=True)
class _Followup:
    proof: _Proof
    session_key: str
    session_id: str
    envelope: dict | None = None


_followup: ContextVar[_Followup | None] = ContextVar('confirmed_reset_followup', default=None)


def capture_reset_proof(runner, event):
    """Keep native callback proof on the event held by confirmation, never in metadata."""
    if event.get_command() not in {'new', 'reset'}:
        return
    from gateway.session_ingress_context import capture_provenance
    provenance = capture_provenance(runner, event)
    if provenance is not None:
        event._confirmed_reset_proof = _Proof(runner, copy(event.source), deepcopy(provenance))


def carry_reset_proof(event, rewritten):
    if rewritten.source is event.source and (proof := getattr(event, '_confirmed_reset_proof', None)) is not None:
        rewritten._confirmed_reset_proof = proof


@contextmanager
def reset_followup_scope(runner, event, session_key, session_id):
    proof = getattr(event, '_confirmed_reset_proof', None)
    if not isinstance(proof, _Proof) or proof.runner is not runner or not session_id:
        yield
        return
    token = _followup.set(_Followup(proof, session_key, session_id))
    try:
        yield
    finally:
        _followup.reset(token)


@asynccontextmanager
async def validate_reset_followup(authority, session_key, session_id):
    """Recheck connector ownership and authorization only when a reset hook queues input."""
    current = _followup.get()
    if current is None or current.session_key != session_key or current.session_id != session_id:
        yield None
        return
    from gateway.session_envelope import _snapshot_native
    from gateway.session_ingress_context import reauthorize_roles
    from hermes_state_runtime import RuntimeStoreError
    if authority is None or current.proof.runner is not authority.runner:
        raise RuntimeStoreError('not_found')
    source = copy(current.proof.source)
    event = MessageEvent(text='', source=source, allow_gateway_control=False)
    fresh_roles = await reauthorize_roles(authority.runner, source, current.proof.provenance)
    payload = _snapshot_native(authority.runner, event, current.proof.provenance, fresh_roles)
    token = _followup.set(replace(current, envelope=payload['native_text_v1']))
    try:
        yield source
    finally:
        _followup.reset(token)


def reset_followup_envelope(authority, entry, identity):
    """Ordinary plugin turns still require a prior admission; only this reset can bootstrap."""
    current = _followup.get()
    if (current is None or current.proof.runner is not authority.runner
            or current.session_key != entry.session_key or current.session_id != entry.session_id
            or current.envelope is None):
        return None
    producer = json.loads(identity)
    return current.envelope if isinstance(producer, list) and producer[:1] == ['plugin'] else None
