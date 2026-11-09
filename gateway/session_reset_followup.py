"""A command reset's own ingress proof, lent once to a plugin follow-up into the session it opened.

Under the session authority a plugin turn borrows the connector provenance of a human message already
committed on the route (``committed_route_envelope``). A ``/new`` or ``/reset`` is never committed (a
command is not native input) and leaves the fresh session with nothing to borrow, so a plugin that
completes the reset with a first prompt (``on_session_reset`` + ``inject_message(expected_session_id=)``)
borrows the reset command's own proof instead: captured while its connector callback is live, held for
that event through confirmation, and lent only to the ``on_session_reset`` call of that reset, for the
route and session id its rotation produced under the old owner's lock (``route_owner_lock``). The drain
revalidates the admitted row like any other automation (``check_automation_route``).

Proofs and lent envelopes live in side tables keyed by the event object, never on the event or in any
value a plugin hook receives."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy, deepcopy
from dataclasses import dataclass
import logging
import weakref

logger = logging.getLogger("gateway.run")

_RESET_COMMANDS = frozenset({'new', 'reset'})


class _EventTable:
    """A private value per live event object (events are unhashable dataclasses)."""

    def __init__(self):
        self._held = {}

    def set(self, event, value):
        key = id(event)
        self._held[key] = (weakref.ref(event, lambda _ref: self._held.pop(key, None)), value)

    def get(self, event):
        held = self._held.get(id(event))
        return held[1] if held is not None and held[0]() is event else None


_proofs = _EventTable()
_envelopes = _EventTable()


@dataclass(frozen=True)
class _Proof:
    runner: object
    source: object
    provenance: dict


@dataclass
class _Lease:
    proof: _Proof
    session_key: str
    session_id: str
    spent: bool = False


_lease: ContextVar[_Lease | None] = ContextVar('reset_followup_lease', default=None)


def capture_reset_proof(runner, event):
    """At ingress, while the connector callback still names *event*: keep its provenance for it.
    Fails closed: any capture failure leaves the reset without a proof, never stops its ingress."""
    if event.get_command() not in _RESET_COMMANDS:
        return
    from gateway.session_ingress_context import capture_provenance
    try:
        provenance = capture_provenance(runner, event)
    except Exception as exc:  # health: allow BLE001 -- a reset must proceed; without a proof no follow-up is lent
        logger.warning("Reset follow-up proof not captured: %s", exc)
        return
    if provenance is not None:
        _proofs.set(event, _Proof(runner, copy(event.source), deepcopy(provenance)))


def carry_reset_proof(event, rewritten):
    """A ``pre_gateway_dispatch`` rewrite keeps the proof only on the source the callback authenticated."""
    proof = _proofs.get(event)
    if proof is not None and rewritten.source is event.source:
        _proofs.set(rewritten, proof)


@contextmanager
def reset_followup_scope(runner, event, session_key, session_id):
    """Lend *event*'s proof to injections scheduled from this reset's ``on_session_reset`` hooks. A task
    spawned inside the hook copies the context and inherits the lease too; it stays single-use and
    bound to this exact route and session id."""
    proof = _proofs.get(event)
    if proof is None or proof.runner is not runner or not session_id:
        yield
        return
    token = _lease.set(_Lease(proof, session_key, session_id))
    try:
        yield
    finally:
        _lease.reset(token)


async def reset_followup(runner, session_key, session_id):
    """``(source, envelope)`` of the resetting actor for the one follow-up this reset lends, rechecked
    against the live connector and current authorization; ``None`` outside a lease. Raises
    ``RuntimeStoreError`` when the actor lost access since the command.

    Unlike an ordinary plugin injection (``_plugin_injection_authorized`` never counts adapter-time
    grants), this path deliberately counts a Discord role grant, but only one re-fetched from the
    connector now (``reauthorize_roles``): the human who issued the reset is the author of its first
    prompt, and a role-only member may reset."""
    lease = _lease.get()
    if (lease is None or lease.spent or lease.proof.runner is not runner
            or lease.session_key != session_key or lease.session_id != session_id):
        return None
    lease.spent = True
    from gateway.platforms.event import MessageEvent
    from gateway.session_envelope import _snapshot_native
    from gateway.session_ingress_context import reauthorize_roles
    source = copy(lease.proof.source)
    fresh_roles = await reauthorize_roles(runner, source, lease.proof.provenance)
    payload = _snapshot_native(runner, MessageEvent(text='', source=source, allow_gateway_control=False),
                               lease.proof.provenance, fresh_roles)
    return source, payload['native_text_v1']


def lend_envelope(event, envelope):
    """Hand the follow-up's validated envelope to ``snapshot_automation`` for this automation event."""
    if envelope is not None:
        _envelopes.set(event, envelope)


def lent_envelope(event):
    return _envelopes.get(event)
