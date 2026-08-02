import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from plugins.platforms.discord.kanban_mirror.config import MirrorConfig
from plugins.platforms.discord.kanban_mirror.daemon import (
    _do_create_thread, _observe_and_reconcile, _record_repair_diagnostic,
    reconcile, run_mirror_daemon, tick,
)
from plugins.platforms.discord.kanban_mirror.planner import Op
from plugins.platforms.discord.kanban_mirror.reconciliation import list_reconciliation_findings, resolve_thread_quarantine
from plugins.platforms.discord.kanban_mirror.state import (
    BoardSnapshot, Card, active_thread_binding, add_member, backfill_legacy_bindings,
    connect_mirror, create_initiative, is_thread_quarantined, load_mirror_state, set_thread,
 set_archived, set_thread_with_binding,
 )


class FakeClient:
    def __init__(self):
        self.fail = set()
        self.sent = {}
        self.archived = False

    def get_channel(self, channel_id):
        if channel_id == "forum":
            return {"available_tags": [{"id": "tag", "name": "active"}]}
        if channel_id in self.fail:
            raise RuntimeError("isolated read failure")
        return {"id": channel_id, "name": "Card", "applied_tags": ["tag"],
                "thread_metadata": {"archived": self.archived}}

    def get_message(self, channel_id, message_id):
        if channel_id in self.fail:
            raise RuntimeError("isolated read failure")
        return {"id": message_id, "content": "body"}

    def send_message(self, channel_id, *, content, nonce=None):
        return self.sent.setdefault(nonce, {"id": f"notice-{len(self.sent) + 1}", "content": content})

    def create_forum_thread(self, channel_id, *, name, content, tag_ids, attachments=None):
        return {"id": "new-thread", "message": {"id": "new-starter"}}


class MissingThreadClient(FakeClient):
    def get_channel(self, channel_id):
        from plugins.platforms.discord.kanban_mirror.discord_client import DiscordAPIError

        if channel_id == "forum":
            return super().get_channel(channel_id)
        raise DiscordAPIError("GET", f"/channels/{channel_id}", 404, "not found")


def seed(path, thread="thread", task="task"):
    conn = connect_mirror(path)
    create_initiative(conn, f"init-{thread}", "Card")
    add_member(conn, f"init-{thread}", task)
    set_thread(conn, f"init-{thread}", thread, f"starter-{thread}")
    backfill_legacy_bindings(conn, "board")
    return conn


def empty_snapshot():
    return BoardSnapshot({}, {}, {}, {}, {})


def test_live_malformed_state_quarantines_and_notice_is_logged_without_posting(tmp_path, caplog):
    conn = seed(tmp_path / "mirror.db")
    client = FakeClient()
    cfg = MirrorConfig(board="board", forum_channel_id="forum", reconciliation_enabled=True)

    with caplog.at_level("WARNING"):
        asyncio.run(_observe_and_reconcile(cfg, client, conn, empty_snapshot(), []))
        asyncio.run(_observe_and_reconcile(cfg, client, conn, empty_snapshot(), []))

    assert is_thread_quarantined(conn, "thread")
    assert client.sent == {}
    notices = [record.message for record in caplog.records if "repair required" in record.message]
    assert len(notices) == 1
    assert "binding.card_missing" in notices[0]
    assert conn.execute("SELECT message_id FROM mirror_repair_notices").fetchone()[0] == ""
    assert not resolve_thread_quarantine(conn, "thread")


def test_changed_quarantine_conflict_emits_one_new_log_without_posting(tmp_path, caplog):
    conn = seed(tmp_path / "mirror.db")
    client = FakeClient()
    cfg = MirrorConfig(board="board", forum_channel_id="forum", reconciliation_enabled=True)

    with caplog.at_level("WARNING"):
        asyncio.run(_observe_and_reconcile(cfg, client, conn, empty_snapshot(), []))
        client.archived = True
        card = Card("task", "Card", "body", "running", "high", None, None, None, None,
                    "1", None, None, None)
        snapshot = BoardSnapshot({"task": card}, {}, {}, {}, {})
        asyncio.run(_observe_and_reconcile(cfg, client, conn, snapshot, []))
        asyncio.run(_observe_and_reconcile(cfg, client, conn, snapshot, []))

    notices = [record.message for record in caplog.records if "repair required" in record.message]
    assert len(notices) == 2
    assert "binding.card_missing" in notices[0]
    assert "thread.premature_archive" in notices[1]
    assert client.sent == {}
    assert conn.execute("SELECT count(*) FROM mirror_repair_notices").fetchone()[0] == 1


def test_changed_repair_diagnostic_is_claimed_once_across_connections(tmp_path):
    path = tmp_path / "mirror.db"
    conn = connect_mirror(path)
    conn.execute(
        "INSERT INTO mirror_repair_notices VALUES (?,?,?,?,?,?)",
        ("thread", 10, "old", "old-nonce", "", 10),
    )
    conn.commit()
    conn.close()

    def claim():
        worker = connect_mirror(path)
        try:
            return _record_repair_diagnostic(
                worker,
                thread_id="thread",
                quarantined_at=10,
                finding_identity="new",
                nonce="new-nonce",
                published_at=20,
            )
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: claim(), range(8)))

    assert claimed.count(True) == 1
    assert claimed.count(False) == 7


def test_legacy_discord_repair_notice_is_claimed_once_for_local_logging(tmp_path):
    conn = connect_mirror(tmp_path / "mirror.db")
    conn.execute(
        "INSERT INTO mirror_repair_notices VALUES (?,?,?,?,?,?)",
        ("thread", 10, "same", "old-nonce", "discord-message", 10),
    )
    conn.commit()

    first = _record_repair_diagnostic(
        conn,
        thread_id="thread",
        quarantined_at=10,
        finding_identity="same",
        nonce="new-nonce",
        published_at=20,
    )
    second = _record_repair_diagnostic(
        conn,
        thread_id="thread",
        quarantined_at=10,
        finding_identity="same",
        nonce="new-nonce",
        published_at=21,
    )

    assert first is True
    assert second is False
    assert tuple(conn.execute(
        "SELECT message_id,published_at FROM mirror_repair_notices"
    ).fetchone()) == ("", 20)


def test_partial_thread_snapshot_does_not_resolve_and_other_thread_continues(tmp_path):
    conn = seed(tmp_path / "mirror.db", "broken", "missing")
    create_initiative(conn, "init-good", "Card")
    add_member(conn, "init-good", "also-missing")
    set_thread(conn, "init-good", "good", "starter-good")
    backfill_legacy_bindings(conn, "board")
    client = FakeClient()
    cfg = MirrorConfig(board="board", forum_channel_id="forum", reconciliation_enabled=True)
    asyncio.run(_observe_and_reconcile(cfg, client, conn, empty_snapshot(), []))
    assert client.sent == {}
    assert conn.execute("SELECT count(*) FROM mirror_repair_notices").fetchone()[0] == 2

    client.fail.add("broken")
    asyncio.run(_observe_and_reconcile(cfg, client, conn, empty_snapshot(), []))
    assert is_thread_quarantined(conn, "broken")
    assert is_thread_quarantined(conn, "good")
    assert client.sent == {}
    assert conn.execute("SELECT count(*) FROM mirror_repair_notices").fetchone()[0] == 2


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


def test_daemon_clean_live_scan_resolves_deterministic_stale_quarantine(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    client = FakeClient()
    cfg = MirrorConfig(board="board", forum_channel_id="forum", reconciliation_enabled=True)
    asyncio.run(_observe_and_reconcile(cfg, client, conn, empty_snapshot(), []))
    assert is_thread_quarantined(conn, "thread")
    card = Card("task", "Card", "body", "running", "high", None, None, None, None,
                "1", None, None, None)
    log = []

    asyncio.run(_observe_and_reconcile(
        cfg, client, conn, BoardSnapshot({"task": card}, {}, {}, {}, {}), log,
    ))

    assert not is_thread_quarantined(conn, "thread")
    assert "reconciliation: RECOVERED quarantine=1" in log


def test_startup_reconcile_preserves_archived_missing_thread_mapping(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    set_archived(conn, "init-thread", 123)

    asyncio.run(reconcile(
        MirrorConfig(board="board", forum_channel_id="forum"), MissingThreadClient(), conn,
    ))

    row = conn.execute(
        "SELECT thread_id,starter_message_id FROM mirror_initiatives WHERE id='init-thread'"
    ).fetchone()
    assert tuple(row) == ("thread", "starter-thread")


def test_observation_skips_archived_memberless_historical_mapping(tmp_path):
    conn = connect_mirror(tmp_path / "mirror.db")
    create_initiative(conn, "historical", "Historical")
    set_thread(conn, "historical", "missing", "starter-missing")
    set_archived(conn, "historical", 123)
    log = []

    asyncio.run(_observe_and_reconcile(
        MirrorConfig(board="board", forum_channel_id="forum", reconciliation_enabled=True),
        MissingThreadClient(), conn, empty_snapshot(), log,
    ))

    assert log == []
    row = conn.execute(
        "SELECT thread_id,starter_message_id FROM mirror_initiatives WHERE id='historical'"
    ).fetchone()
    assert tuple(row) == ("missing", "starter-missing")


def test_tick_backfills_bindings_before_startup_reconciliation(tmp_path, monkeypatch):
    conn = connect_mirror(tmp_path / "mirror.db")
    create_initiative(conn, "init-thread", "Card")
    add_member(conn, "init-thread", "task")
    set_thread(conn, "init-thread", "thread", "starter-thread")
    card = Card("task", "Card", "body", "running", "high", None, None, None, None,
                "1", None, None, None)
    snapshot = BoardSnapshot({"task": card}, {}, {}, {}, {})
    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon.load_board_snapshot",
        lambda board: snapshot,
    )
    monkeypatch.setattr("plugins.platforms.discord.kanban_mirror.daemon.plan", lambda *args: [])
    cfg = MirrorConfig(
        board="board", forum_channel_id="forum", reconciliation_enabled=True,
        binding_transitions_enabled=True, terminal_lifecycle_enabled=False,
    )
    client = FakeClient()

    log = asyncio.run(tick(cfg, client, conn, allow_llm=False))

    assert "binding_initialization: backfilled 1" in log
    assert active_thread_binding(conn, "thread").task_id == "task"
    assert "binding.open_count" not in {f.code for f in list_reconciliation_findings(conn, open_only=True)}
    assert not is_thread_quarantined(conn, "thread")
    assert client.sent == {}


def test_tick_recovers_transitions_before_live_reconciliation(tmp_path, monkeypatch):
    conn = connect_mirror(tmp_path / "mirror.db")
    order = []

    async def recover(*args):
        order.append("recover")

    async def observe(*args, **kwargs):
        order.append("reconcile")

    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon.load_board_snapshot",
        lambda board: empty_snapshot(),
    )
    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon._recover_binding_transitions", recover,
    )
    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon._observe_and_reconcile", observe,
    )
    monkeypatch.setattr("plugins.platforms.discord.kanban_mirror.daemon.plan", lambda *args: [])
    cfg = MirrorConfig(
        board="board", forum_channel_id="forum", reconciliation_enabled=True,
        binding_transitions_enabled=True, automatic_successor_enabled=False,
        terminal_lifecycle_enabled=False,
    )

    asyncio.run(tick(cfg, FakeClient(), conn, allow_llm=False))

    assert order == ["recover", "reconcile"]


def test_tick_audits_active_threads_when_live_reconciliation_is_enabled(tmp_path, monkeypatch):
    conn = connect_mirror(tmp_path / "mirror.db")
    order = []

    async def observe(*args, **kwargs):
        order.append("reconcile")

    async def audit(*args, **kwargs):
        order.append("audit")
        return False

    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon.load_board_snapshot",
        lambda board: empty_snapshot(),
    )
    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon._observe_and_reconcile", observe,
    )
    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon._audit_active_threads", audit,
    )
    monkeypatch.setattr("plugins.platforms.discord.kanban_mirror.daemon.plan", lambda *args: [])
    cfg = MirrorConfig(
        board="board", forum_channel_id="forum", guild_id="guild",
        reconciliation_enabled=True, automatic_successor_enabled=False,
        terminal_lifecycle_enabled=False,
    )

    asyncio.run(tick(cfg, FakeClient(), conn, allow_llm=False))

    assert order == ["reconcile", "audit"]


def test_tick_aborts_all_planning_when_post_observation_board_reload_fails(tmp_path, monkeypatch):
    conn = connect_mirror(tmp_path / "mirror.db")
    calls = [0]

    def load(_board):
        calls[0] += 1
        if calls[0] == 1:
            return empty_snapshot()
        raise sqlite3.OperationalError("board changed during observation")

    planned = []
    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon.load_board_snapshot", load,
    )
    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon.plan",
        lambda *args: planned.append(True) or [],
    )
    cfg = MirrorConfig(
        board="board", forum_channel_id="forum", reconciliation_enabled=True,
        automatic_successor_enabled=False, terminal_lifecycle_enabled=False,
    )

    log = asyncio.run(tick(cfg, FakeClient(), conn, allow_llm=False))

    assert log[-1] == "reconciliation: FAILED"
    assert planned == []


def test_daemon_startup_recovers_transitions_before_live_reconciliation(tmp_path, monkeypatch):
    conn = connect_mirror(tmp_path / "mirror.db")
    order = []
    cfg = MirrorConfig(
        enabled=True, board="board", forum_channel_id="forum", guild_id="guild",
        reconciliation_enabled=True, binding_transitions_enabled=True,
    )

    async def recover(*args):
        order.append("recover")

    async def observe(*args, **kwargs):
        order.append("reconcile")

    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.config.load_mirror_config", lambda: cfg,
    )
    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon.load_discord_token", lambda *a, **k: "token",
    )
    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon.DiscordClient", lambda token: FakeClient(),
    )
    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon.connect_mirror", lambda path: conn,
    )
    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon.load_board_snapshot",
        lambda board: empty_snapshot(),
    )
    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon._recover_binding_transitions", recover,
    )
    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon._observe_and_reconcile", observe,
    )

    asyncio.run(run_mirror_daemon(lambda: False))

    assert order == ["recover", "reconcile"]


def test_new_thread_mapping_and_binding_are_created_atomically(tmp_path):
    conn = connect_mirror(tmp_path / "mirror.db")
    create_initiative(conn, "init-task", "Card")
    add_member(conn, "init-task", "task")
    card = Card("task", "Card", "body", "running", "high", None, None, None, None,
                "1", None, None, None)
    snapshot = BoardSnapshot({"task": card}, {}, {}, {}, {})
    state = load_mirror_state(conn)
    op = Op("create_thread", {
        "initiative_id": "init-task", "title": "Card", "body": "body", "tags": ["active"],
    })
    cfg = MirrorConfig(board="board", forum_channel_id="forum")

    asyncio.run(_do_create_thread(cfg, FakeClient(), conn, snapshot, state, op, False, []))

    initiative = load_mirror_state(conn)["init-task"]
    binding = active_thread_binding(conn, "new-thread")
    assert (initiative.thread_id, initiative.starter_message_id) == ("new-thread", "new-starter")
    assert binding is not None and binding.task_id == "task" and binding.board_slug == "board"
    assert binding.starter_revision_hash


def test_grouped_thread_binding_reconciles_when_bound_card_is_a_member(tmp_path):
    conn = connect_mirror(tmp_path / "mirror.db")
    create_initiative(conn, "group", "Grouped")
    add_member(conn, "group", "done")
    add_member(conn, "group", "active")
    cards = {
        "done": Card("done", "Done", "body", "done", "high", None, None, None, None,
                     "1", "2", None, None),
        "active": Card("active", "Card", "body", "running", "high", None, None, None, None,
                       "1", None, None, None),
    }
    snapshot = BoardSnapshot(cards, {}, {}, {}, {})
    state = load_mirror_state(conn)
    op = Op("create_thread", {
        "initiative_id": "group", "title": "Card", "body": "body", "tags": ["active"],
    })
    cfg = MirrorConfig(board="board", forum_channel_id="forum", reconciliation_enabled=True)
    client = FakeClient()

    asyncio.run(_do_create_thread(cfg, client, conn, snapshot, state, op, False, []))
    asyncio.run(_observe_and_reconcile(cfg, client, conn, snapshot, []))

    binding = active_thread_binding(conn, "new-thread")
    codes = {f.code for f in list_reconciliation_findings(conn, open_only=True)}
    assert binding is not None and binding.task_id == "active"
    assert "binding.mapping_missing" not in codes
    assert not is_thread_quarantined(conn, "new-thread")


def test_atomic_thread_binding_rolls_back_mapping_when_membership_is_ambiguous(tmp_path):
    conn = connect_mirror(tmp_path / "mirror.db")
    create_initiative(conn, "group", "Grouped")
    add_member(conn, "group", "one")
    add_member(conn, "group", "two")

    try:
        set_thread_with_binding(conn, "group", "thread", "starter", "board", "not-a-member", "hash")
    except ValueError as exc:
        assert "not an initiative member" in str(exc)
    else:
        raise AssertionError("ambiguous membership should fail closed")

    initiative = load_mirror_state(conn)["group"]
    assert initiative.thread_id is None and initiative.starter_message_id is None
    assert conn.execute("SELECT COUNT(*) FROM mirror_binding_epochs").fetchone()[0] == 0


def test_live_starter_must_match_intended_cosmetic_edit(tmp_path):
    from plugins.platforms.discord.kanban_mirror.daemon import _verified_starter_payload

    cfg = MirrorConfig(board="board", forum_channel_id="forum")
    client = FakeClient()
    client.get_message = lambda channel_id, message_id: {"id": message_id, "content": "altered"}

    try:
        asyncio.run(_verified_starter_payload(
            client, cfg, "thread", "starter",
            {"title": "Card", "body": "body", "tags": ["active"]},
        ))
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("altered live starter was accepted")
