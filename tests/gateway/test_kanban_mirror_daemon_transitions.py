import asyncio

from gateway.kanban_mirror.config import MirrorConfig
from gateway.kanban_mirror.daemon import (
    DiscordTransitionPublisher, _initiate_automatic_successors, _recover_binding_transitions,
    _starter_identity_authorized,
)
from gateway.kanban_mirror.state import (
    BoardSnapshot, Card, active_thread_binding, add_member, backfill_legacy_bindings, connect_mirror,
    create_initiative, get_binding_transition, load_mirror_state, prepare_binding_transition, set_thread,
)


class FakeDiscordClient:
    def __init__(self):
        self.messages = {("thread", "thread"): {"id": "thread", "content": "old"}}
        self.thread = {"id": "thread", "name": "Old", "applied_tags": ["doing-id"]}
        self.forum = {"id": "forum", "available_tags": [
            {"id": "doing-id", "name": "doing"}, {"id": "active-id", "name": "active"},
            {"id": "ready-id", "name": "ready"}, {"id": "waiting-id", "name": "waiting"},
            {"id": "ops-id", "name": "ops"}
        ]}
        self.by_nonce = {}
        self.events = []

    def send_message(self, channel_id, *, content, nonce=None):
        self.events.append(("publish", nonce))
        return self.by_nonce.setdefault(nonce, {"id": "note-1", "content": content})

    def get_channel(self, channel_id):
        return self.forum if channel_id == "forum" else dict(self.thread)

    def get_message(self, channel_id, message_id):
        return dict(self.messages[(channel_id, message_id)])

    def update_message(self, channel_id, message_id, *, content):
        self.events.append(("starter", content))
        self.messages[(channel_id, message_id)] = {"id": message_id, "content": content}
        return self.messages[(channel_id, message_id)]

    def update_thread(self, thread_id, *, name=None, tag_ids=None, **kwargs):
        self.events.append(("thread", name, tag_ids))
        if name is not None:
            self.thread["name"] = name
        if tag_ids is not None:
            self.thread["applied_tags"] = tag_ids
        return dict(self.thread)


def seed(path):
    conn = connect_mirror(path)
    create_initiative(conn, "init", "Old")
    add_member(conn, "init", "old")
    set_thread(conn, "init", "thread", "thread")
    return conn


def test_concrete_publisher_receipt_nonce_and_live_starter():
    client = FakeDiscordClient()
    publisher = DiscordTransitionPublisher(client, MirrorConfig(forum_channel_id="forum"))
    payload = {"content": "Old -> New"}
    first = publisher.publish_transition("thread", payload, operation_key="move")
    second = publisher.publish_transition("thread", payload, operation_key="move")
    assert first == second
    assert len(client.by_nonce) == 1
    publisher.update_starter("thread", {"title": "New", "body": "next", "tags": ["active"]})
    assert publisher.read_starter("thread") == {"title": "New", "body": "next", "tags": ["active"]}


def test_startup_backfill_and_pending_resume_without_duplicate_epoch(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    cfg = MirrorConfig(board="board", forum_channel_id="forum", binding_transitions_enabled=True)
    client = FakeDiscordClient()
    asyncio.run(_recover_binding_transitions(cfg, client, conn, []))
    assert active_thread_binding(conn, "thread").task_id == "old"

    prepare_binding_transition(
        conn, transition_key="move", thread_id="thread",
        old_card_metadata={"board_slug": "board", "task_id": "old"},
        new_card_metadata={"board_slug": "board", "task_id": "new"},
        transition_payload={"content": "Old -> New"},
        starter_payload={"title": "New", "body": "next", "tags": ["active"]},
    )
    # Cosmetic updates for the represented old card remain safe, while a
    # direct successor rewrite is blocked until the transition is confirmed.
    assert _starter_identity_authorized(conn, "thread", "old")
    assert not _starter_identity_authorized(conn, "thread", "new")
    asyncio.run(_recover_binding_transitions(cfg, client, conn, []))
    asyncio.run(_recover_binding_transitions(cfg, client, conn, []))
    assert get_binding_transition(conn, "move").state == "starter_verified"
    assert active_thread_binding(conn, "thread").task_id == "new"
    assert conn.execute("SELECT count(*) FROM mirror_binding_epochs").fetchone()[0] == 2
    assert len(client.by_nonce) == 1


def _card(task, status, owner="ops"):
    return Card(task, task.title(), "body", status, "normal", owner, None, None, None,
                "1", "2" if status == "done" else None, None, None)


def test_automatic_successor_orders_transition_and_is_restart_idempotent(tmp_path):
    conn = seed(tmp_path / "auto.db"); backfill_legacy_bindings(conn, "board")
    snap = BoardSnapshot({"old": _card("old", "done"), "new": _card("new", "ready")},
                         {"old": ["new"]}, {"new": ["old"]}, {}, {})
    cfg = MirrorConfig(board="board", forum_channel_id="forum", binding_transitions_enabled=True,
                       automatic_successor_enabled=True)
    client = FakeDiscordClient()
    asyncio.run(_initiate_automatic_successors(cfg, client, conn, snap, load_mirror_state(conn), []))
    asyncio.run(_initiate_automatic_successors(cfg, client, conn, snap, load_mirror_state(conn), []))
    assert active_thread_binding(conn, "thread").task_id == "new"
    assert [event[0] for event in client.events] == ["publish", "starter", "thread"]
    assert len(client.by_nonce) == 1
    assert conn.execute("SELECT task_id FROM mirror_members").fetchone()[0] == "new"


def test_automatic_successor_fanout_fails_closed_with_stable_finding(tmp_path):
    conn = seed(tmp_path / "fan.db"); backfill_legacy_bindings(conn, "board")
    cards = {"old": _card("old", "done"), "a": _card("a", "ready"), "b": _card("b", "ready")}
    snap = BoardSnapshot(cards, {"old": ["b", "a"]}, {"a": ["old"], "b": ["old"]}, {}, {})
    cfg = MirrorConfig(board="board", forum_channel_id="forum", binding_transitions_enabled=True,
                       automatic_successor_enabled=True)
    client = FakeDiscordClient()
    for _ in range(2):
        asyncio.run(_initiate_automatic_successors(cfg, client, conn, snap, load_mirror_state(conn), []))
    assert conn.execute("SELECT task_id FROM mirror_binding_epochs WHERE state='open'").fetchone()[0] == "old"
    assert not client.by_nonce
    rows = conn.execute("SELECT code FROM mirror_reconciliation_findings WHERE resolved_at IS NULL").fetchall()
    assert [row[0] for row in rows] == ["successor.selection_ambiguous"]
