import asyncio

from plugins.platforms.discord.kanban_mirror.config import MirrorConfig
from plugins.platforms.discord.kanban_mirror.daemon import _observe_and_reconcile
from plugins.platforms.discord.kanban_mirror.reconciliation import list_reconciliation_findings, resolve_thread_quarantine
from plugins.platforms.discord.kanban_mirror.state import (
    BoardSnapshot, Card, add_member, backfill_legacy_bindings, connect_mirror,
    create_initiative, is_thread_quarantined, set_thread,
)


class FakeClient:
    def __init__(self):
        self.fail = set()
        self.sent = {}

    def get_channel(self, channel_id):
        if channel_id == "forum":
            return {"available_tags": [{"id": "tag", "name": "active"}]}
        if channel_id in self.fail:
            raise RuntimeError("isolated read failure")
        return {"id": channel_id, "name": "Card", "applied_tags": ["tag"],
                "thread_metadata": {"archived": False}}

    def get_message(self, channel_id, message_id):
        if channel_id in self.fail:
            raise RuntimeError("isolated read failure")
        return {"id": message_id, "content": "body"}

    def send_message(self, channel_id, *, content, nonce=None):
        return self.sent.setdefault(nonce, {"id": f"notice-{len(self.sent) + 1}", "content": content})


def seed(path, thread="thread", task="task"):
    conn = connect_mirror(path)
    create_initiative(conn, f"init-{thread}", "Card")
    add_member(conn, f"init-{thread}", task)
    set_thread(conn, f"init-{thread}", thread, f"starter-{thread}")
    backfill_legacy_bindings(conn, "board")
    return conn


def empty_snapshot():
    return BoardSnapshot({}, {}, {}, {}, {})


def test_live_malformed_state_quarantines_and_notice_is_deduplicated(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    client = FakeClient()
    cfg = MirrorConfig(board="board", forum_channel_id="forum", reconciliation_enabled=True)

    asyncio.run(_observe_and_reconcile(cfg, client, conn, empty_snapshot(), []))
    asyncio.run(_observe_and_reconcile(cfg, client, conn, empty_snapshot(), []))

    assert is_thread_quarantined(conn, "thread")
    assert len(client.sent) == 1
    notice = next(iter(client.sent.values()))["content"]
    assert "binding.card_missing" in notice and "without remapping, archiving, or deleting" in notice
    assert not resolve_thread_quarantine(conn, "thread")


def test_partial_thread_snapshot_does_not_resolve_and_other_thread_continues(tmp_path):
    conn = seed(tmp_path / "mirror.db", "broken", "missing")
    create_initiative(conn, "init-good", "Card")
    add_member(conn, "init-good", "also-missing")
    set_thread(conn, "init-good", "good", "starter-good")
    backfill_legacy_bindings(conn, "board")
    client = FakeClient()
    cfg = MirrorConfig(board="board", forum_channel_id="forum", reconciliation_enabled=True)
    asyncio.run(_observe_and_reconcile(cfg, client, conn, empty_snapshot(), []))
    assert len(client.sent) == 2

    client.fail.add("broken")
    asyncio.run(_observe_and_reconcile(cfg, client, conn, empty_snapshot(), []))
    assert is_thread_quarantined(conn, "broken")
    assert is_thread_quarantined(conn, "good")
    assert len(client.sent) == 2


def test_daemon_builds_live_metadata_expectations_without_false_quarantine(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    client = FakeClient()
    cfg = MirrorConfig(board="board", forum_channel_id="forum", reconciliation_enabled=True)
    card = Card("task", "Card", "body", "running", "high", None, None, None, None,
                "1", None, None, None)
    snapshot = BoardSnapshot({"task": card}, {}, {}, {}, {})
    asyncio.run(_observe_and_reconcile(cfg, client, conn, snapshot, []))
    codes = {f.code for f in list_reconciliation_findings(conn, open_only=True)}
    assert "thread.tags_mismatch" in codes
    assert not is_thread_quarantined(conn, "thread")
    assert client.sent == {}
