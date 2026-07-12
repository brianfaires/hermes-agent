"""Recoverable controller orchestration for Discord binding transitions.

This module is deliberately not wired into the production daemon yet.  It
coordinates the durable state machine with a small, fakeable publishing port;
the port must implement idempotent transition publishing by operation key.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from .state import (
    BindingTransition,
    authorize_starter_update,
    confirm_binding_transition,
    prepare_binding_transition,
    verify_starter_revision,
)


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _payload_hash(value: dict) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TransitionReceipt:
    """Proof returned by the publishing port for an idempotent send."""

    message_id: str
    thread_id: str
    operation_key: str
    payload_hash: str


class TransitionPublisher(Protocol):
    """Discord/mirror side effects needed by the transition controller.

    ``publish_transition`` must deduplicate by ``operation_key`` and return the
    original receipt on retry. ``read_starter`` must return the live Discord
    title/body/tags representation, not a local cache.
    """

    def publish_transition(
        self, thread_id: str, payload: dict, *, operation_key: str
    ) -> TransitionReceipt: ...

    def update_starter(self, thread_id: str, payload: dict) -> None: ...

    def read_starter(self, thread_id: str) -> dict: ...


def run_binding_transition(
    conn: sqlite3.Connection,
    publisher: TransitionPublisher,
    *,
    transition_key: str,
    thread_id: str,
    old_card_metadata: dict,
    new_card_metadata: dict,
    transition_payload: dict,
    starter_payload: dict,
) -> BindingTransition:
    """Advance one transition, resuming safely after any completed boundary.

    Ordering is fixed: freeze, publish and validate receipt, atomically switch
    epochs, update starter, read live starter, then persist its revision.  Any
    exception is intentionally propagated so a controller retry can resume
    from the durable state.
    """
    transition = prepare_binding_transition(
        conn,
        transition_key=transition_key,
        thread_id=thread_id,
        old_card_metadata=old_card_metadata,
        new_card_metadata=new_card_metadata,
        transition_payload=transition_payload,
        starter_payload=starter_payload,
    )

    if transition.state == "prepared":
        receipt = publisher.publish_transition(
            transition.thread_id,
            transition.transition_payload,
            operation_key=transition.transition_key,
        )
        expected_payload_hash = _payload_hash(transition.transition_payload)
        if (
            not isinstance(receipt, TransitionReceipt)
            or not receipt.message_id.strip()
            or receipt.thread_id != transition.thread_id
            or receipt.operation_key != transition.transition_key
            or receipt.payload_hash != expected_payload_hash
        ):
            raise ValueError("Discord transition receipt does not match frozen publish")
        transition = confirm_binding_transition(
            conn, transition.transition_key, receipt.message_id
        )

    if transition.state == "message_confirmed":
        payload, expected_revision = authorize_starter_update(
            conn, transition.transition_key
        )
        publisher.update_starter(transition.thread_id, payload)
        live = publisher.read_starter(transition.thread_id)
        if not isinstance(live, dict) or _payload_hash(live) != expected_revision:
            raise ValueError("live starter does not match frozen starter payload")
        transition = verify_starter_revision(
            conn, transition.transition_key, expected_revision
        )

    return transition
