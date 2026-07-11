from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from typing import cast

from gateway.kanban_mirror.closed_thread_policy import classify_thread_state
from gateway.kanban_mirror.config import MirrorConfig, load_mirror_config
from gateway.kanban_mirror.daemon import _audit_active_threads, _do_archive_thread, _do_edit_post, _do_post_note, _publish_edit, _send_with_closed_thread_policy
from gateway.kanban_mirror.discord_client import DiscordClient
from gateway.kanban_mirror.planner import Op, current_publish_hash
from gateway.kanban_mirror.state import BoardSnapshot, Card, Initiative, MemberState


def mk_initiative(thread_id="th1"):
    return Initiative(
        id="init_t1",
        title="A card",
        kind="post",
        thread_id=thread_id,
        starter_message_id="m1",
        brief=None,
        needs_you=None,
        blocked_reasons={},
        published_hash=None,
        brief_stale=False,
        brief_updated_at=None,
        archived_at=None,
        created_at=1,
        updated_at=1,
        members={"t1": MemberState("t1", None, None)},
    )


class FakeClient:
    def __init__(self, state="active", *, last_message_ts="2026-07-06T00:00:00+00:00", active_threads=None, messages=None):
        self.state = state
        self.last_message_ts = last_message_ts
        self.active_threads = active_threads or []
        self.messages = messages or {}
        self.sent = []
        self.updated = []
        self.message_updates = []
        self.dm_created = []

    def get_channel(self, channel_id):
        if channel_id == "missing":
            from gateway.kanban_mirror.discord_client import DiscordAPIError
            raise DiscordAPIError("GET", f"/channels/{channel_id}", 404, "not found")
        if self.state == "active":
            return {
                "id": channel_id,
                "last_message_id": "msg-latest",
                "thread_metadata": {"archived": False, "locked": False},
                "available_tags": [
                    {"id": "tag-running", "name": "running"},
                    {"id": "tag-ops", "name": "ops"},
                ],
            }
        if self.state == "archived":
            return {"id": channel_id, "thread_metadata": {"archived": True, "locked": False}}
        if self.state == "locked":
            return {"id": channel_id, "thread_metadata": {"archived": False, "locked": True}}
        if self.state == "archived_locked":
            return {"id": channel_id, "thread_metadata": {"archived": True, "locked": True}}
        return {}

    def create_dm(self, user_id):
        self.dm_created.append(user_id)
        return {"id": "dm1"}

    def get_message(self, channel_id, message_id):
        if (channel_id, message_id) in self.messages:
            return self.messages[(channel_id, message_id)]
        return {"id": message_id, "channel_id": channel_id, "timestamp": self.last_message_ts}

    def get_current_user(self):
        return {"id": "mirror-bot"}

    def list_active_threads(self, guild_id):
        return self.active_threads

    def update_thread(self, thread_id, **kwargs):
        self.updated.append((thread_id, kwargs))
        if kwargs.get("archive") is False:
            self.state = "active"
        if kwargs.get("locked") is False:
            self.state = "active"
        return {"id": thread_id, "thread_metadata": {"archived": False, "locked": False}}

    def update_message(self, channel_id, message_id, *, content):
        self.message_updates.append((channel_id, message_id, content))
        return {"id": message_id, "channel_id": channel_id, "content": content}

    def update_forum_tags(self, channel_id, available_tags):
        return {"id": channel_id, "available_tags": available_tags}

    def send_message(self, channel_id, *, content):
        self.sent.append((channel_id, content))
        return {"id": f"msg{len(self.sent)}"}


class FailingRedirectClient(FakeClient):
    def create_dm(self, user_id):
        raise RuntimeError("dm failed")


class MissingMessageClient(FakeClient):
    def update_message(self, channel_id, message_id, *, content):
        from gateway.kanban_mirror.discord_client import DiscordAPIError

        raise DiscordAPIError("PATCH", f"/channels/{channel_id}/messages/{message_id}", 404, '{"message":"Unknown Message"}')


def test_closed_thread_policy_loads_from_config():
    cfg = load_mirror_config({
        "kanban": {"discord_mirror": {
            "enabled": True,
            "board": "operations",
            "forum_channel_id": "forum1",
            "closed_thread_reply_policy": {
                "default_action": "discard",
                "states": {"archived": "discard", "locked": "discard", "missing": "discard"},
                "rules": [{
                    "match": {"thread_state": "archived", "source": "live_reply"},
                    "action": "redirect",
                    "destination": {"platform": "discord", "kind": "dm", "user_id": "188008666304086017"},
                }],
                "failure_policy": {"redirect_failure": "log_only", "reopen_failure": "log_and_kanban_comment"},
            },
            "done_thread_archive_idle_minutes": 17,
        }}
    })
    assert cfg.closed_thread_reply_policy.default_action == "discard"
    assert cfg.closed_thread_reply_policy.rules[0].action == "redirect"
    assert cfg.closed_thread_reply_policy.rules[0].destination["user_id"] == "188008666304086017"
    assert cfg.done_thread_archive_idle_minutes == 17


def test_archived_reply_discards_by_default_and_does_not_send_to_original_thread():
    client = FakeClient("archived")
    log = []
    handled, action, message_id = asyncio.run(_send_with_closed_thread_policy(
        MirrorConfig(enabled=True, board="operations", forum_channel_id="forum1"),
        cast(DiscordClient, client),
        mk_initiative(),
        source="live_reply",
        content="note body",
        task_id="t1",
        log=log,
    ))
    assert handled is True
    assert action == "discard"
    assert message_id == ""
    assert client.sent == []
    assert "action=discard" in log[0]


def test_archived_reply_redirects_to_discord_dm_with_origin_header():
    cfg = load_mirror_config({"kanban": {"discord_mirror": {
        "enabled": True,
        "board": "operations",
        "forum_channel_id": "forum1",
        "closed_thread_reply_policy": {"rules": [{
            "match": {"thread_state": "archived", "source": "live_reply"},
            "action": "redirect",
            "destination": {"platform": "discord", "kind": "dm", "user_id": "188008666304086017"},
        }]},
    }}})
    client = FakeClient("archived")
    handled, action, message_id = asyncio.run(_send_with_closed_thread_policy(
        cfg, cast(DiscordClient, client), mk_initiative(), source="live_reply", content="note body", task_id="t1", log=[]
    ))
    assert handled is True
    assert action == "redirect"
    assert message_id == "msg1"
    assert client.sent[0][0] == "dm1"
    assert "Origin: Hermes Kanban Discord mirror board=operations card=t1" in client.sent[0][1]
    assert "original_thread=th1" in client.sent[0][1]
    assert "note body" in client.sent[0][1]
    assert client.dm_created == ["188008666304086017"]


def test_archived_reply_reopen_verifies_before_original_thread_send():
    cfg = load_mirror_config({"kanban": {"discord_mirror": {
        "enabled": True,
        "board": "operations",
        "forum_channel_id": "forum1",
        "closed_thread_reply_policy": {"rules": [{
            "match": {"thread_state": "archived", "source": "live_reply"},
            "action": "reopen_thread",
        }]},
    }}})
    client = FakeClient("archived")
    handled, action, message_id = asyncio.run(_send_with_closed_thread_policy(
        cfg, cast(DiscordClient, client), mk_initiative(), source="live_reply", content="note body", task_id="t1", log=[]
    ))
    assert handled is True
    assert action == "reopen_thread"
    assert message_id == "msg1"
    assert client.updated == [("th1", {"archive": False, "locked": False})]
    assert client.sent == [("th1", "note body")]


def test_publish_edit_updates_starter_message_inside_thread_channel():
    client = FakeClient("active")

    ok = asyncio.run(_publish_edit(
        cast(DiscordClient, client),
        MirrorConfig(enabled=True, board="operations", forum_channel_id="forum1"),
        mk_initiative(),
        "Updated title",
        "Updated body",
        ["running", "ops"],
    ))

    assert ok is True
    assert client.message_updates == [("th1", "m1", "Updated body")]
    assert client.updated == [("th1", {"name": "Updated title", "tag_ids": ["tag-running", "tag-ops"]})]


def test_missing_starter_message_clears_mapping_instead_of_retrying_forever():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("""
        CREATE TABLE mirror_initiatives (
            id TEXT PRIMARY KEY,
            thread_id TEXT,
            starter_message_id TEXT,
            published_hash TEXT,
            updated_at INTEGER
        )
    """)
    conn.execute(
        "INSERT INTO mirror_initiatives (id, thread_id, starter_message_id, published_hash, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("init_t1", "th1", "m1", "oldhash", 1),
    )
    initiative = mk_initiative()
    log = []

    asyncio.run(_do_edit_post(
        MirrorConfig(enabled=True, board="operations", forum_channel_id="forum1"),
        cast(DiscordClient, MissingMessageClient("active")),
        conn,
        BoardSnapshot({}, {}, {}, {}, {}),
        {initiative.id: initiative},
        Op("edit_post", {"initiative_id": initiative.id, "title": "Updated", "body": "Body", "tags": ["running"]}),
        False,
        log,
    ))

    row = conn.execute("SELECT thread_id, starter_message_id, published_hash FROM mirror_initiatives WHERE id='init_t1'").fetchone()
    assert row == (None, None, None)
    assert any("CLEARED stale thread mapping" in line for line in log)


def test_discarded_post_note_records_note_so_it_does_not_retry():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE mirror_notes (note_key TEXT PRIMARY KEY, initiative_id TEXT, message_id TEXT, posted_at INTEGER)")
    card = Card(
        id="t1", title="Done", body="", status="done", priority=0, assignee=None,
        branch_name=None, workspace_kind="scratch", created_by="agent", created_at=1,
        completed_at=None, last_failure_error=None, result=None,
    )
    snapshot = BoardSnapshot({"t1": card}, {}, {}, {}, {})
    initiative = mk_initiative()
    op = Op("post_note", {"initiative_id": initiative.id, "note_key": "done:t1", "note_kind": "member_done", "task_id": "t1"})
    log = []

    asyncio.run(_do_post_note(
        MirrorConfig(enabled=True, board="operations", forum_channel_id="forum1"),
        cast(DiscordClient, FakeClient("archived")),
        conn,
        snapshot,
        {initiative.id: initiative},
        op,
        False,
        False,
        log,
    ))

    row = conn.execute("SELECT message_id FROM mirror_notes WHERE note_key='done:t1'").fetchone()
    assert row is not None
    assert row[0] == "discarded:done:t1"


def test_locked_classification_wins_when_thread_is_archived_and_locked():
    assert classify_thread_state({"thread_metadata": {"archived": True, "locked": True}}) == "locked"


def test_redirect_failure_policy_can_comment_on_kanban(monkeypatch):
    import hermes_cli.kanban_db as kanban_db

    comments = []

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(kanban_db, "connect", lambda *, board: FakeConn())
    monkeypatch.setattr(
        kanban_db,
        "add_comment",
        lambda conn, task_id, author, message: comments.append((task_id, author, message)),
    )
    cfg = load_mirror_config({"kanban": {"discord_mirror": {
        "enabled": True,
        "board": "operations",
        "forum_channel_id": "forum1",
        "closed_thread_reply_policy": {
            "rules": [{
                "match": {"thread_state": "archived", "source": "live_reply"},
                "action": "redirect",
                "destination": {"platform": "discord", "kind": "dm", "user_id": "188008666304086017"},
            }],
            "failure_policy": {"redirect_failure": "log_and_kanban_comment"},
        },
    }}})

    handled, action, message_id = asyncio.run(_send_with_closed_thread_policy(
        cfg,
        cast(DiscordClient, FailingRedirectClient("archived")),
        mk_initiative(),
        source="live_reply",
        content="note body",
        task_id="t1",
        log=[],
    ))

    assert handled is False
    assert action == "redirect_error"
    assert message_id == ""
    assert len(comments) == 1
    assert comments[0][0:2] == ("t1", "ops")
    assert "redirect failed" in comments[0][2]


def test_redirect_failure_policy_log_only_does_not_comment(monkeypatch):
    import hermes_cli.kanban_db as kanban_db

    comments = []

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(kanban_db, "connect", lambda *, board: FakeConn())
    monkeypatch.setattr(
        kanban_db,
        "add_comment",
        lambda conn, task_id, author, message: comments.append((task_id, author, message)),
    )
    cfg = load_mirror_config({"kanban": {"discord_mirror": {
        "enabled": True,
        "board": "operations",
        "forum_channel_id": "forum1",
        "closed_thread_reply_policy": {
            "rules": [{
                "match": {"thread_state": "archived", "source": "live_reply"},
                "action": "redirect",
                "destination": {"platform": "discord", "kind": "dm", "user_id": "188008666304086017"},
            }],
            "failure_policy": {"redirect_failure": "log_only"},
        },
    }}})

    handled, action, _message_id = asyncio.run(_send_with_closed_thread_policy(
        cfg,
        cast(DiscordClient, FailingRedirectClient("archived")),
        mk_initiative(),
        source="live_reply",
        content="note body",
        task_id="t1",
        log=[],
    ))

    assert handled is False
    assert action == "redirect_error"
    assert comments == []


def test_missing_thread_reopen_failure_policy_comments_on_kanban(monkeypatch):
    import hermes_cli.kanban_db as kanban_db

    comments = []

    class FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(kanban_db, "connect", lambda *, board: FakeConn())
    monkeypatch.setattr(
        kanban_db,
        "add_comment",
        lambda conn, task_id, author, message: comments.append((task_id, author, message)),
    )
    cfg = load_mirror_config({"kanban": {"discord_mirror": {
        "enabled": True,
        "board": "operations",
        "forum_channel_id": "forum1",
        "closed_thread_reply_policy": {
            "rules": [{"match": {"thread_state": "missing", "source": "live_reply"}, "action": "reopen_thread"}],
            "failure_policy": {"reopen_failure": "log_and_kanban_comment"},
        },
    }}})

    handled, action, message_id = asyncio.run(_send_with_closed_thread_policy(
        cfg,
        cast(DiscordClient, FakeClient()),
        mk_initiative("missing"),
        source="live_reply",
        content="note body",
        task_id="t1",
        log=[],
    ))

    assert handled is False
    assert action == "reopen_error"
    assert message_id == ""
    assert len(comments) == 1
    assert comments[0][0:2] == ("t1", "ops")
    assert "thread is missing" in comments[0][2]


def _archive_test_conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("""
        CREATE TABLE mirror_initiatives (
            id TEXT PRIMARY KEY,
            title TEXT,
            kind TEXT,
            thread_id TEXT,
            starter_message_id TEXT,
            archived_at INTEGER,
            created_at INTEGER,
            updated_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE mirror_members (
            task_id TEXT PRIMARY KEY,
            initiative_id TEXT NOT NULL,
            last_status TEXT,
            last_sig TEXT
        )
    """)
    conn.execute("""
        INSERT INTO mirror_initiatives
            (id, title, kind, thread_id, starter_message_id, archived_at, created_at, updated_at)
        VALUES ('init_t1', 'A card', 'post', 'th1', 'm1', NULL, 1, 1)
    """)
    conn.execute("INSERT INTO mirror_members VALUES ('t1', 'init_t1', NULL, NULL)")
    return conn


def _done_snapshot():
    card = Card(
        id="t1", title="Done", body="", status="done", priority=0, assignee=None,
        branch_name=None, workspace_kind="scratch", created_by="agent", created_at=1,
        completed_at=1900, last_failure_error=None, result=None,
    )
    return BoardSnapshot({"t1": card}, {}, {}, {}, {})


def _published_done_initiative():
    snapshot = _done_snapshot()
    cfg = MirrorConfig(enabled=True, board="operations", forum_channel_id="forum1", done_thread_archive_idle_minutes=1)
    initiative = mk_initiative()
    return Initiative(
        id=initiative.id,
        title=initiative.title,
        kind=initiative.kind,
        thread_id=initiative.thread_id,
        starter_message_id=initiative.starter_message_id,
        brief=initiative.brief,
        needs_you=initiative.needs_you,
        blocked_reasons=initiative.blocked_reasons,
        published_hash=current_publish_hash(initiative, snapshot, cfg),
        brief_stale=initiative.brief_stale,
        brief_updated_at=initiative.brief_updated_at,
        archived_at=initiative.archived_at,
        created_at=initiative.created_at,
        updated_at=initiative.updated_at,
        members=initiative.members,
    )


def test_done_thread_archive_waits_until_idle_delay(monkeypatch):
    monkeypatch.setattr("gateway.kanban_mirror.daemon.time.time", lambda: 2000.0)
    conn = _archive_test_conn()
    client = FakeClient("active", last_message_ts="1970-01-01T00:32:50+00:00")
    log = []

    asyncio.run(_do_archive_thread(
        MirrorConfig(enabled=True, board="operations", forum_channel_id="forum1", done_thread_archive_idle_minutes=1),
        cast(DiscordClient, client),
        conn,
        _done_snapshot(),
        {"init_t1": _published_done_initiative()},
        Op("archive_thread", {"initiative_id": "init_t1"}),
        False,
        log,
    ))

    assert client.updated == []
    assert conn.execute("SELECT archived_at FROM mirror_initiatives WHERE id='init_t1'").fetchone()[0] is None
    assert any("idle 30s < required 60s" in line for line in log)


def test_done_thread_archive_runs_after_idle_delay(monkeypatch):
    monkeypatch.setattr("gateway.kanban_mirror.daemon.time.time", lambda: 2000.0)
    conn = _archive_test_conn()
    client = FakeClient("active", last_message_ts="1970-01-01T00:30:00+00:00")
    log = []

    asyncio.run(_do_archive_thread(
        MirrorConfig(enabled=True, board="operations", forum_channel_id="forum1", done_thread_archive_idle_minutes=1),
        cast(DiscordClient, client),
        conn,
        _done_snapshot(),
        {"init_t1": _published_done_initiative()},
        Op("archive_thread", {"initiative_id": "init_t1"}),
        False,
        log,
    ))

    assert client.updated == [("th1", {"archive": True})]
    assert conn.execute("SELECT archived_at FROM mirror_initiatives WHERE id='init_t1'").fetchone()[0] == 2000
    assert "archive_thread: init_t1" in log


def test_new_message_resets_done_thread_archive_idle_timer(monkeypatch):
    now = {"value": 2000.0}
    monkeypatch.setattr("gateway.kanban_mirror.daemon.time.time", lambda: now["value"])
    conn = _archive_test_conn()
    client = FakeClient("active", last_message_ts="1970-01-01T00:32:50+00:00")
    log = []

    asyncio.run(_do_archive_thread(
        MirrorConfig(enabled=True, board="operations", forum_channel_id="forum1", done_thread_archive_idle_minutes=1),
        cast(DiscordClient, client),
        conn,
        _done_snapshot(),
        {"init_t1": _published_done_initiative()},
        Op("archive_thread", {"initiative_id": "init_t1"}),
        False,
        log,
    ))
    now["value"] = 2070.0
    asyncio.run(_do_archive_thread(
        MirrorConfig(enabled=True, board="operations", forum_channel_id="forum1", done_thread_archive_idle_minutes=1),
        cast(DiscordClient, client),
        conn,
        _done_snapshot(),
        {"init_t1": _published_done_initiative()},
        Op("archive_thread", {"initiative_id": "init_t1"}),
        False,
        log,
    ))

    assert client.updated == [("th1", {"archive": True})]
    assert any("idle 30s < required 60s" in line for line in log)


def test_done_thread_archive_skips_until_done_tag_publish_is_current(monkeypatch):
    monkeypatch.setattr("gateway.kanban_mirror.daemon.time.time", lambda: 2000.0)
    conn = _archive_test_conn()
    client = FakeClient("active", last_message_ts="1970-01-01T00:30:00+00:00")
    log = []

    asyncio.run(_do_archive_thread(
        MirrorConfig(enabled=True, board="operations", forum_channel_id="forum1", done_thread_archive_idle_minutes=1),
        cast(DiscordClient, client),
        conn,
        _done_snapshot(),
        {"init_t1": mk_initiative()},
        Op("archive_thread", {"initiative_id": "init_t1"}),
        False,
        log,
    ))

    assert client.updated == []
    assert conn.execute("SELECT archived_at FROM mirror_initiatives WHERE id='init_t1'").fetchone()[0] is None
    assert "archive_thread: SKIPPED init_t1 (pending publish)" in log


def test_active_thread_audit_reopens_archived_terminal_mapping_for_repair():
    conn = _archive_test_conn()
    conn.execute("UPDATE mirror_initiatives SET archived_at = 123 WHERE id='init_t1'")
    initiative = replace(_published_done_initiative(), archived_at=123)
    client = FakeClient(active_threads=[{"id": "th1", "parent_id": "forum1"}])
    log = []

    changed = asyncio.run(_audit_active_threads(
        MirrorConfig(enabled=True, board="operations", forum_channel_id="forum1", guild_id="guild1"),
        cast(DiscordClient, client),
        conn,
        _done_snapshot(),
        {"init_t1": initiative},
        log,
    ))

    assert changed is True
    assert conn.execute("SELECT archived_at FROM mirror_initiatives WHERE id='init_t1'").fetchone()[0] is None
    assert "active_thread_audit: REPAIR init_t1 thread=th1" in log


def test_active_thread_audit_adopts_unmapped_bot_thread_with_one_known_card():
    conn = _archive_test_conn()
    conn.execute("UPDATE mirror_initiatives SET archived_at=123 WHERE id='init_t1'")
    client = FakeClient(
        active_threads=[{"id": "orphan", "parent_id": "forum1"}],
        messages={("orphan", "orphan"): {
            "id": "orphan",
            "content": "Work items\n🔴 Security follow-up\n`ops · P0 · t1 · updated just now`",
            "author": {"id": "mirror-bot", "bot": True},
        }},
    )
    log = []

    changed = asyncio.run(_audit_active_threads(
        MirrorConfig(enabled=True, board="operations", forum_channel_id="forum1", guild_id="guild1"),
        cast(DiscordClient, client),
        conn,
        _done_snapshot(),
        {"init_t1": replace(mk_initiative(), archived_at=123)},
        log,
    ))

    row = conn.execute(
        "SELECT thread_id, starter_message_id, archived_at FROM mirror_initiatives WHERE id='init_t1'"
    ).fetchone()
    assert changed is True
    assert row == ("orphan", "orphan", None)
    assert "active_thread_audit: ADOPT init_t1 thread=orphan task=t1" in log


def test_active_thread_audit_refuses_ambiguous_or_foreign_orphan_threads():
    conn = _archive_test_conn()
    snapshot = _done_snapshot()
    snapshot.cards["t2"] = replace(snapshot.cards["t1"], id="t2", title="Other")
    client = FakeClient(
        active_threads=[
            {"id": "ambiguous", "parent_id": "forum1"},
            {"id": "foreign", "parent_id": "forum1"},
        ],
        messages={
            ("ambiguous", "ambiguous"): {
                "id": "ambiguous",
                "content": "Cards `t1` and `t2`",
                "author": {"id": "mirror-bot", "bot": True},
            },
            ("foreign", "foreign"): {
                "id": "foreign",
                "content": "Card `t1`",
                "author": {"id": "other-bot", "bot": True},
            },
        },
    )
    log = []

    changed = asyncio.run(_audit_active_threads(
        MirrorConfig(enabled=True, board="operations", forum_channel_id="forum1", guild_id="guild1"),
        cast(DiscordClient, client), conn, snapshot, {"init_t1": mk_initiative()}, log,
    ))

    assert changed is False
    assert "active_thread_audit: UNMAPPED ambiguous" in log
    assert "active_thread_audit: UNMAPPED foreign" in log


def test_active_thread_audit_refuses_two_orphans_for_the_same_card():
    conn = _archive_test_conn()
    messages = {
        (thread_id, thread_id): {
            "id": thread_id,
            "content": "Card `t1`",
            "author": {"id": "mirror-bot", "bot": True},
        }
        for thread_id in ("orphan-a", "orphan-b")
    }
    client = FakeClient(
        active_threads=[
            {"id": "orphan-a", "parent_id": "forum1"},
            {"id": "orphan-b", "parent_id": "forum1"},
        ],
        messages=messages,
    )
    log = []

    changed = asyncio.run(_audit_active_threads(
        MirrorConfig(enabled=True, board="operations", forum_channel_id="forum1", guild_id="guild1"),
        cast(DiscordClient, client), conn, _done_snapshot(), {"init_t1": mk_initiative()}, log,
    ))

    assert changed is False
    assert conn.execute("SELECT thread_id FROM mirror_initiatives WHERE id='init_t1'").fetchone()[0] == "th1"
    assert "active_thread_audit: UNMAPPED orphan-a" in log
    assert "active_thread_audit: UNMAPPED orphan-b" in log
