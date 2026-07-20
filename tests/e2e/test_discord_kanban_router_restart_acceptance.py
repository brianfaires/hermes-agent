"""Restart acceptance at the executable Discord/Kanban router seams.

Only Discord transport is faked.  Configuration, profile resolution and both
SQLite stores use their production loaders and schemas under a temporary
HERMES_HOME; every restart closes and reopens durable state.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import yaml

from plugins.platforms.discord.kanban_mirror.inbox import (
    DiscordReplyContext,
    handle_reply,
    load_config,
    validate_router_config,
)
from plugins.platforms.discord.kanban_mirror.config import MirrorConfig
from plugins.platforms.discord.kanban_mirror.daemon import (
    _initiate_automatic_successors,
    _recover_binding_transitions,
    _resume_terminal_lifecycles,
)
from plugins.platforms.discord.kanban_mirror.lifecycle import get_terminal_lifecycle
from plugins.platforms.discord.kanban_mirror.reconciliation import reconcile_mirror_state, resolve_thread_quarantine
from plugins.platforms.discord.kanban_mirror.outbox import OutboundEnvelope, enqueue, get
from plugins.platforms.discord.kanban_mirror.recovery import run_outbound_recovery
from plugins.platforms.discord.kanban_mirror.state import (
    active_thread_binding,
    add_member,
    backfill_legacy_bindings,
    connect_mirror,
    create_initiative,
    get_binding_transition,
    mirror_db_path,
    prepare_binding_transition,
    set_thread,
    BoardSnapshot, Card, is_thread_quarantined, load_mirror_state,
)
from hermes_cli import kanban_db as kb


class DiscordTransport:
    """Stateful network boundary shared by pre/post-restart process objects."""

    def __init__(self):
        self.messages = {("thread-1", "thread-1"): {"id": "thread-1", "content": "old"}}
        self.thread = {"id": "thread-1", "name": "Old", "applied_tags": ["doing-id"]}
        self.forum = {"id": "forum-1", "available_tags": [
            {"id": "doing-id", "name": "doing"}, {"id": "active-id", "name": "active"}
        ]}
        self.by_nonce = {}
        self.events = []
        self.fail_transition = False

    def send_message(self, channel_id, *, content, nonce=None):
        self.events.append(("transition", nonce))
        if self.fail_transition:
            raise OSError("Discord unavailable")
        return self.by_nonce.setdefault(nonce, {"id": "transition-1", "content": content})

    def get_channel(self, channel_id):
        return self.forum if channel_id == "forum-1" else dict(self.thread)

    def get_message(self, channel_id, message_id):
        return dict(self.messages[(channel_id, message_id)])

    def update_forum_tags(self, channel_id, tags):
        realized = []
        for index, tag in enumerate(tags):
            realized.append({**tag, "id": tag.get("id", f"created-{index}")})
        self.forum["available_tags"] = realized
        return dict(self.forum)

    def update_message(self, channel_id, message_id, *, content):
        self.events.append(("starter", content))
        self.messages[(channel_id, message_id)] = {"id": message_id, "content": content}
        return self.messages[(channel_id, message_id)]

    def update_thread(self, thread_id, *, name=None, tag_ids=None, **_kwargs):
        self.events.append(("thread", name, tag_ids))
        if name is not None:
            self.thread["name"] = name
        if tag_ids is not None:
            self.thread["applied_tags"] = tag_ids
        return dict(self.thread)


class LifecycleDiscordTransport:
    """Concrete Discord network boundary whose state survives worker objects."""

    def __init__(self):
        self.forum = {"id": "forum-1", "available_tags": [{"id": "done-id", "name": "done"}]}
        self.channels = {
            "thread-1": {"id": "thread-1", "applied_tags": [], "last_message_id": "human-0", "archived": False},
            "digest-thread": {"id": "digest-thread", "applied_tags": [], "last_message_id": "digest-starter", "pinned": False},
        }
        self.messages = {
            ("thread-1", "human-0"): {"id": "human-0", "content": "finished", "timestamp": "1970-01-01T00:01:30Z"},
            ("digest-thread", "digest-starter"): {"id": "digest-starter", "content": "Board digest"},
        }
        self.nonces = {}
        self.operations = []

    def get_channel(self, channel_id):
        return self.forum if channel_id == "forum-1" else dict(self.channels[channel_id])

    def get_message(self, channel_id, message_id):
        return dict(self.messages[(channel_id, message_id)])

    def send_message(self, channel_id, *, content, nonce=None):
        if nonce not in self.nonces:
            self.operations.append("summary")
            self.nonces[nonce] = {"id": "summary-1", "content": content}
        return dict(self.nonces[nonce])

    def update_message(self, channel_id, message_id, *, content):
        self.operations.append("digest")
        self.messages[(channel_id, message_id)] = {"id": message_id, "content": content}
        return dict(self.messages[(channel_id, message_id)])

    def update_thread(self, thread_id, *, tag_ids=None, pinned=None, archive=None, **_kwargs):
        channel = self.channels[thread_id]
        if tag_ids is not None:
            self.operations.append("tag")
            channel["applied_tags"] = list(tag_ids)
        if pinned is not None:
            channel["pinned"] = pinned
        if archive is not None:
            self.operations.append("archive")
            channel["archived"] = archive
        return dict(channel)


@pytest.fixture
def isolated_router(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # A real second profile is required by production identity validation.
    (home / "profiles" / "reviewer").mkdir(parents=True)
    raw = {
        "gateway": {"multiplex_profiles": True},
        "discord": {"kanban_reply_inbox": {
            "enabled": True,
            "conversation_router_enabled": True,
            "conversation_log_enabled": True,
            "forum_channel_ids": ["forum-1"],
            "board_slug": "acceptance",
            "conversation_router_ingress_bot_id": "100",
            "profile_bot_user_ids": {"100": "default", "200": "reviewer"},
        }},
    }
    (home / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    kb.init_db(board="acceptance")
    board = kb.connect(board="acceptance")
    old = kb.create_task(board, title="Old", body="work", assignee="reviewer", board="acceptance")
    new = kb.create_task(board, title="New", body="next", assignee="reviewer", board="acceptance")
    board.close()

    mirror = connect_mirror(mirror_db_path("acceptance"))
    create_initiative(mirror, "initiative", "Old")
    add_member(mirror, "initiative", old)
    set_thread(mirror, "initiative", "thread-1", "thread-1")
    backfill_legacy_bindings(mirror, "acceptance")
    mirror.close()
    return home, old, new


def test_disabled_default_and_unrelated_text_voice_are_not_claimed():
    cfg = load_config({})
    assert not cfg.enabled and not cfg.conversation_router_enabled
    unrelated = DiscordReplyContext(
        message_id="plain", author_id="7", author_label="human",
        forum_channel_id="ordinary-text", thread_id="channel", content="hello",
    )
    assert handle_reply(unrelated, config=cfg).reason == "disabled"
    # Voice/non-thread events cannot even become a router context; this is the
    # adapter contract that leaves the existing voice pipeline untouched.
    from plugins.platforms.discord.kanban_mirror.inbox import context_from_discord_message
    assert context_from_discord_message(SimpleNamespace(id="voice", channel=SimpleNamespace(id="voice"))) is None


@pytest.mark.asyncio
async def test_conversation_outbox_log_and_transition_resume_across_restart(isolated_router):
    _home, old, new = isolated_router
    cfg = load_config()  # production config.yaml loader
    assert validate_router_config(cfg, multiplex_profiles=True) == "default"

    human = DiscordReplyContext(
        message_id="human-1", author_id="7", author_label="Fixture Human",
        forum_channel_id="forum-1", thread_id="thread-1", content="please review",
        discord_created_at=123, message_link="https://discord.invalid/messages/human-1",
        attachments=({"filename": "evidence.txt", "url": "https://discord.invalid/evidence"},),
    )
    routed = handle_reply(human, config=cfg)
    assert not routed.consumed and routed.reason == "conversation_routed"
    assert routed.route_profile == "reviewer"

    # The human ledger is committed before the caller can dispatch. Freeze an
    # answer, fail delivery, close every connection, and recover in a new worker.
    mirror = connect_mirror(mirror_db_path("acceptance"))
    event = mirror.execute("SELECT * FROM mirror_conversation_events WHERE discord_message_id='human-1'").fetchone()
    assert event["event_class"] == "conversation.human" and event["discord_message_link"]
    operation = enqueue(mirror, OutboundEnvelope(
        profile="reviewer", thread_id="thread-1", reply_to_message_id="human-1",
        content="frozen owner answer", attachments=(), correlation_id=routed.correlation_id,
        binding_key=event["binding_key"],
    ))
    mirror.close()

    class Adapter:
        def __init__(self, connected=True):
            self.is_connected = connected
            self.sent = []

    adapter = Adapter(connected=False)
    async def send(_adapter, payload):
        adapter.sent.append(dict(payload))
        return SimpleNamespace(success=True, message_id="agent-1")

    restarted = connect_mirror(mirror_db_path("acceptance"))
    recovery_args = dict(
        worker_id="process-2", adapters={"reviewer": adapter}, send=send,
        transition_publishers={}, clock=lambda: 1_000, base_backoff=0,
    )
    await run_outbound_recovery(restarted, **recovery_args)
    assert get(restarted, operation)["status"] == "failed"
    adapter.is_connected = True
    await run_outbound_recovery(restarted, **recovery_args)
    assert get(restarted, operation)["status"] == "delivered"
    assert len(adapter.sent) == 1 and adapter.sent[0]["content"] == "frozen owner answer"
    agent = restarted.execute("SELECT * FROM mirror_conversation_events WHERE discord_message_id='agent-1'").fetchone()
    assert agent["event_class"] == "conversation.agent" and agent["replied_to_message_id"] == "human-1"
    restarted.close()

    # !log current exports both sides with metadata through the real Kanban DB.
    logged = handle_reply(DiscordReplyContext(
        message_id="log-1", author_id="7", author_label="Fixture Human",
        forum_channel_id="forum-1", thread_id="thread-1", content="!log current",
    ), config=cfg)
    assert logged.reason == "handled" and logged.action == "log"
    board = kb.connect(board="acceptance")
    body = board.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC", (old,)).fetchone()[0]
    assert "please review" in body and "frozen owner answer" in body and "evidence.txt" in body
    board.close()

    # Prepare while Discord is down.  A recreated daemon resumes the durable
    # transition, confirms its note before replacing the starter, then switches binding.
    mirror = connect_mirror(mirror_db_path("acceptance"))
    prepare_binding_transition(
        mirror, transition_key="acceptance-move", thread_id="thread-1",
        old_card_metadata={"board_slug": "acceptance", "task_id": old},
        new_card_metadata={"board_slug": "acceptance", "task_id": new},
        transition_payload={"content": "Old -> New"},
        starter_payload={"title": "New", "body": "next", "tags": ["active"]},
    )
    mirror.close()
    transport = DiscordTransport(); transport.fail_transition = True
    first = connect_mirror(mirror_db_path("acceptance"))
    with pytest.raises(OSError, match="Discord unavailable"):
        await _recover_binding_transitions(
            MirrorConfig(forum_channel_id="forum-1", binding_transitions_enabled=True),
            transport, first, [],
        )
    assert get_binding_transition(first, "acceptance-move").state == "prepared"
    assert active_thread_binding(first, "thread-1").task_id == old
    first.close()

    transport.fail_transition = False
    second = connect_mirror(mirror_db_path("acceptance"))
    await _recover_binding_transitions(MirrorConfig(forum_channel_id="forum-1", binding_transitions_enabled=True), transport, second, [])
    assert get_binding_transition(second, "acceptance-move").state == "starter_verified"
    assert active_thread_binding(second, "thread-1").task_id == new
    assert [event[0] for event in transport.events[-3:]] == ["transition", "starter", "thread"]
    assert len(transport.by_nonce) == 1
    second.close()


@pytest.mark.asyncio
async def test_fanout_quarantine_needs_clean_restart_and_explicit_resolution(isolated_router):
    """An ambiguous successor can neither alter the starter nor self-clear quarantine."""
    _home, old, _new = isolated_router
    def card(task_id, status):
        return Card(task_id, task_id, "body", status, "high", "reviewer", None,
                    None, None, "1", "2" if status == "done" else None, None, None)
    bad = BoardSnapshot(
        {old: card(old, "done"), "a": card("a", "running"), "b": card("b", "running")},
        {old: ("a", "b")}, {"a": (old,), "b": (old,)}, {}, {},
    )
    cfg = MirrorConfig(board="acceptance", forum_channel_id="forum-1",
                       automatic_successor_enabled=True, reconciliation_enabled=True)
    discord = DiscordTransport()
    conn = connect_mirror(mirror_db_path("acceptance"))
    starter_before = dict(discord.messages[("thread-1", "thread-1")])
    await _initiate_automatic_successors(cfg, discord, conn, bad, load_mirror_state(conn), [])
    assert is_thread_quarantined(conn, "thread-1")
    assert discord.messages[("thread-1", "thread-1")] == starter_before
    assert not resolve_thread_quarantine(conn, "thread-1", now=10)
    conn.close()

    # Recreate daemon state/DB connection.  A now-unambiguous scan marks the
    # finding clean but remains fail-closed until explicit acknowledgement.
    clean = BoardSnapshot({old: card(old, "done"), "a": card("a", "running")},
                          {old: ("a",)}, {"a": (old,)}, {}, {})
    restarted = connect_mirror(mirror_db_path("acceptance"))
    # The reconciliation scan is the durable clean observation; it does not
    # itself acknowledge or mutate the quarantined thread.
    reconcile_mirror_state(restarted, observed_threads={},
                           cards=[("acceptance", old), ("acceptance", "a")], now=15)
    assert is_thread_quarantined(restarted, "thread-1")
    assert discord.messages[("thread-1", "thread-1")] == starter_before
    assert resolve_thread_quarantine(restarted, "thread-1", now=20)
    await _initiate_automatic_successors(cfg, discord, restarted, clean,
                                         load_mirror_state(restarted), [])
    assert active_thread_binding(restarted, "thread-1").task_id == "a"
    assert [event[0] for event in discord.events[-3:]] == ["transition", "starter", "thread"]
    restarted.close()


@pytest.mark.asyncio
async def test_terminal_lifecycle_idle_archive_is_exactly_once_across_restarts(
    isolated_router, monkeypatch
):
    """A bounded terminal lifecycle survives worker replacement without replay."""
    _home, old, _new = isolated_router
    mirror_path = mirror_db_path("acceptance")
    setup = connect_mirror(mirror_path)
    create_initiative(setup, "digest", "Acceptance digest", "digest")
    set_thread(setup, "digest", "digest-thread", "digest-starter")
    setup.close()

    terminal = Card(
        old, "Old", "work", "done", "high", "reviewer", None,
        None, None, "80", "90", None, "accepted",
    )
    snapshot = BoardSnapshot({old: terminal}, {}, {}, {}, {})
    cfg = MirrorConfig(
        board="acceptance", forum_channel_id="forum-1", guild_id="guild-1",
        terminal_lifecycle_enabled=True, done_thread_archive_idle_minutes=1,
    )
    discord = LifecycleDiscordTransport()
    now = {"value": 100}
    monkeypatch.setattr("plugins.platforms.discord.kanban_mirror.daemon.time.time", lambda: now["value"])

    first = connect_mirror(mirror_path)
    await _resume_terminal_lifecycles(cfg, discord, first, snapshot, load_mirror_state(first), [])
    lifecycle_key = first.execute("SELECT lifecycle_key FROM mirror_terminal_lifecycles").fetchone()[0]
    life = get_terminal_lifecycle(first, lifecycle_key)
    assert life.state == "tag_confirmed" and life.archive_due_at == 150
    assert discord.operations == ["summary", "digest", "tag"]
    assert "<!-- terminal:thread-1 -->" in discord.messages[("digest-thread", "digest-starter")]["content"]
    assert "done-id" in discord.channels["thread-1"]["applied_tags"]
    first.close()

    # A real routed human event resets idle without replaying completion work.
    routed = handle_reply(DiscordReplyContext(
        message_id="human-late", author_id="7", author_label="Fixture Human",
        forum_channel_id="forum-1", thread_id="thread-1", content="one more note",
        discord_created_at=130,
    ), config=load_config())
    assert routed.reason == "conversation_routed"
    now["value"] = 140
    second = connect_mirror(mirror_path)
    await _resume_terminal_lifecycles(cfg, discord, second, snapshot, load_mirror_state(second), [])
    life = get_terminal_lifecycle(second, lifecycle_key)
    assert life.state == "tag_confirmed" and life.latest_activity_at == 130
    assert life.archive_due_at == 190 and "archive" not in discord.operations
    second.close()

    # Recreated connections and concrete publisher wrappers honor the new boundary.
    now["value"] = 189
    third = connect_mirror(mirror_path)
    await _resume_terminal_lifecycles(cfg, discord, third, snapshot, load_mirror_state(third), [])
    assert get_terminal_lifecycle(third, lifecycle_key).state == "tag_confirmed"
    third.close()

    now["value"] = 190
    fourth = connect_mirror(mirror_path)
    await _resume_terminal_lifecycles(cfg, discord, fourth, snapshot, load_mirror_state(fourth), [])
    assert get_terminal_lifecycle(fourth, lifecycle_key).state == "archived"
    assert discord.channels["thread-1"]["archived"] is True
    assert load_mirror_state(fourth)["initiative"].archived_at == 190
    assert discord.operations == ["summary", "digest", "tag", "archive"]
    fourth.close()

    now["value"] = 250
    final = connect_mirror(mirror_path)
    await _resume_terminal_lifecycles(cfg, discord, final, snapshot, load_mirror_state(final), [])
    assert get_terminal_lifecycle(final, lifecycle_key).state == "archived"
    assert discord.operations == ["summary", "digest", "tag", "archive"]
    final.close()
