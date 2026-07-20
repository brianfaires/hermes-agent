import hashlib
import json

import pytest

from plugins.platforms.discord.kanban_mirror.state import (
    active_thread_binding,
    add_member,
    backfill_legacy_bindings,
    connect_mirror,
    create_initiative,
    get_binding_transition,
    set_thread,
)
from plugins.platforms.discord.kanban_mirror.transitions import TransitionReceipt, run_binding_transition


def canonical_hash(payload):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def seed(path):
    conn = connect_mirror(path)
    create_initiative(conn, "init", "Fixture")
    add_member(conn, "init", "old")
    set_thread(conn, "init", "thread", "starter")
    backfill_legacy_bindings(conn, "board")
    return conn


def kwargs():
    return dict(
        transition_key="move-1",
        thread_id="thread",
        old_card_metadata={"board_slug": "board", "task_id": "old", "title": "Old"},
        new_card_metadata={"board_slug": "board", "task_id": "new", "title": "New"},
        transition_payload={"content": "Old -> New", "actor": "Ops"},
        starter_payload={"title": "New", "body": "next", "tags": ["active"]},
    )


class FakePublisher:
    def __init__(self):
        self.events = []
        self.receipts = {}
        self.live = {"title": "Old", "body": "old", "tags": ["doing"]}
        self.fail_send = False
        self.fail_update = False
        self.bad_receipt = False
        self.bad_live = False

    def publish_transition(self, thread_id, payload, *, operation_key):
        self.events.append(("publish", operation_key, dict(payload)))
        if self.fail_send:
            raise RuntimeError("send failed")
        if operation_key not in self.receipts:
            self.receipts[operation_key] = TransitionReceipt(
                "discord-1", thread_id, operation_key, canonical_hash(payload)
            )
            self.events.append(("discord_post_created", "discord-1"))
        receipt = self.receipts[operation_key]
        if self.bad_receipt:
            return TransitionReceipt(
                receipt.message_id, thread_id, operation_key, "wrong"
            )
        return receipt

    def update_starter(self, thread_id, payload):
        self.events.append(("update_starter", dict(payload)))
        if self.fail_update:
            raise RuntimeError("update failed")
        self.live = dict(payload)

    def read_starter(self, thread_id):
        self.events.append(("read_starter",))
        if self.bad_live:
            return {**self.live, "body": "stale"}
        return dict(self.live)


def test_orders_discord_confirmation_before_epoch_and_starter(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    publisher = FakePublisher()
    result = run_binding_transition(conn, publisher, **kwargs())

    assert result.state == "starter_verified"
    assert [event[0] for event in publisher.events] == [
        "publish", "discord_post_created", "update_starter", "read_starter"
    ]
    epochs = conn.execute(
        "SELECT task_id,state,transition_message_id,starter_revision_hash "
        "FROM mirror_binding_epochs ORDER BY sequence"
    ).fetchall()
    assert [row[0:3] for row in epochs] == [
        ("old", "closed", "discord-1"), ("new", "open", None)
    ]
    assert epochs[1][3] == canonical_hash(kwargs()["starter_payload"])


def test_send_failure_keeps_old_epoch_and_retry_uses_frozen_idempotency_key(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    publisher = FakePublisher(); publisher.fail_send = True
    with pytest.raises(RuntimeError, match="send failed"):
        run_binding_transition(conn, publisher, **kwargs())
    assert get_binding_transition(conn, "move-1").state == "prepared"
    assert active_thread_binding(conn, "thread").task_id == "old"

    publisher.fail_send = False
    assert run_binding_transition(conn, publisher, **kwargs()).state == "starter_verified"
    publishes = [event for event in publisher.events if event[0] == "publish"]
    assert [event[1] for event in publishes] == ["move-1", "move-1"]
    assert publishes[0][2] == publishes[1][2] == kwargs()["transition_payload"]
    assert len(publisher.receipts) == 1


def test_confirmed_transition_recovers_starter_without_repost_or_duplicate_epoch(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    publisher = FakePublisher(); publisher.fail_update = True
    with pytest.raises(RuntimeError, match="update failed"):
        run_binding_transition(conn, publisher, **kwargs())
    assert get_binding_transition(conn, "move-1").state == "message_confirmed"
    assert active_thread_binding(conn, "thread").task_id == "new"

    publisher.fail_update = False
    assert run_binding_transition(conn, publisher, **kwargs()).state == "starter_verified"
    assert len([e for e in publisher.events if e[0] == "publish"]) == 1
    assert conn.execute("SELECT count(*) FROM mirror_binding_epochs").fetchone()[0] == 2
    event_count = len(publisher.events)
    assert run_binding_transition(conn, publisher, **kwargs()).state == "starter_verified"
    assert len(publisher.events) == event_count


def test_mismatched_receipt_and_live_revision_fail_closed(tmp_path):
    conn = seed(tmp_path / "receipt.db")
    publisher = FakePublisher(); publisher.bad_receipt = True
    with pytest.raises(ValueError, match="receipt does not match"):
        run_binding_transition(conn, publisher, **kwargs())
    assert get_binding_transition(conn, "move-1").state == "prepared"
    assert active_thread_binding(conn, "thread").task_id == "old"

    conn = seed(tmp_path / "live.db")
    publisher = FakePublisher(); publisher.bad_live = True
    with pytest.raises(ValueError, match="live starter does not match"):
        run_binding_transition(conn, publisher, **kwargs())
    assert get_binding_transition(conn, "move-1").state == "message_confirmed"
    assert active_thread_binding(conn, "thread").task_id == "new"
    assert active_thread_binding(conn, "thread").starter_revision_hash is None
