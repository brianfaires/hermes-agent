import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.kanban_watchers import (
    _acquire_singleton_lock,
    _release_singleton_lock,
)
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionEntry
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


class BotIdAdapter(BasePlatformAdapter):
    def __init__(self, bot_id: str):
        super().__init__(
            PlatformConfig(
                enabled=True,
                token=f"{bot_id}-token",
                typing_indicator=False,
            ),
            Platform.TELEGRAM,
        )
        self.bot_id = bot_id
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(
            {
                "bot_id": self.bot_id,
                "chat_id": chat_id,
                "content": content,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id=f"{self.bot_id}-msg")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


async def _run_one_notifier_tick_and_drain(monkeypatch, runner, *adapters):
    await _run_one_notifier_tick(monkeypatch, runner)
    pending = []
    for adapter in adapters:
        pending.extend(
            task
            for task in getattr(adapter, "_background_tasks", set())
            if not task.done()
        )
    if pending:
        await asyncio.gather(*pending)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    # Most tests model the default gateway after its dispatcher acquired the
    # singleton lock. Tests for startup or non-owner gateways clear this.
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _make_real_path_runner(default_adapter, ang_adapter, tmp_path):
    runner = GatewayRunner(
        GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    enabled=True,
                    token="default-token",
                    typing_indicator=False,
                )
            },
            multiplex_profiles=True,
        )
    )
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: default_adapter}
    runner._profile_adapters = {"ang": {Platform.TELEGRAM: ang_adapter}}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_notifier_profile = "ang"
    runner._active_profile_name = lambda: "ang"
    runner._resolve_profile_home_for_source = lambda _source: tmp_path
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._set_session_env = lambda _context: []
    runner._reset_session_env = lambda _tokens: None
    runner._is_user_authorized_for_source = lambda _source: True
    runner._external_drain_active = False
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._cache_session_source = lambda _key, _source: None
    runner._bind_adapter_run_generation = lambda *_args, **_kwargs: None
    runner._is_session_run_current = lambda _key, _generation: True
    runner._reply_anchor_for_event = lambda _event: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(
        side_effect=lambda *, event, **_kwargs: event.text
    )
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._run_post_turn_hooks = AsyncMock()
    runner._clear_durable_active_turn = AsyncMock()
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "agent final from ang",
            "messages": [
                {"role": "user", "content": "wake"},
                {"role": "assistant", "content": "agent final from ang"},
            ],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "api_calls": 1,
            "failed": False,
        }
    )
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    entry = SessionEntry(
        session_key="agent:ang:telegram:dm:shared-dm",
        session_id="sess-ang",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        origin=None,
    )
    runner.session_store = MagicMock()
    runner.session_store._entries = {}
    runner._async_session_store = MagicMock()
    runner._async_session_store._store = runner.session_store
    runner._async_session_store.get_or_create_session = AsyncMock(return_value=entry)
    runner._async_session_store.load_transcript = AsyncMock(return_value=[])
    runner._async_session_store.append_to_transcript = AsyncMock()
    runner._async_session_store.update_session = AsyncMock()
    runner._async_session_store.clear_resume_pending = AsyncMock()
    runner._async_session_store.has_any_sessions = AsyncMock(return_value=True)
    runner._async_session_store.mark_turn_active = AsyncMock(return_value=None)

    for adapter, handler in (
        (default_adapter, runner._primary_message_handler()),
        (ang_adapter, runner._make_profile_message_handler("ang")),
    ):
        adapter.gateway_runner = runner
        adapter.set_message_handler(handler)
        adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    ang_adapter._owner_profile = "ang"
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_replays_telegram_dm_topic_delivery_metadata(tmp_path, monkeypatch):
    db_path = tmp_path / "dm-topic-metadata.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm topic task",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            thread_id="20197",
            delivery_mode="notify+wake",
            delivery_metadata={
                "chat_type": "dm",
                "direct_messages_topic_id": "20197",
                "telegram_dm_topic_reply_fallback": True,
                "telegram_reply_to_message_id": "462",
                "thread_id": "20197",
            },
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["metadata"] == {
        "chat_type": "dm",
        "direct_messages_topic_id": "20197",
        "telegram_dm_topic_reply_fallback": True,
        "telegram_reply_to_message_id": "462",
        "thread_id": "20197",
    }
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "dm"
    assert adapter.handled[0].source.thread_id == "20197"


def test_active_named_profile_subscription_is_delivered(tmp_path, monkeypatch):
    """A sub stamped with the gateway's own named profile uses self.adapters.

    Regression for #71340: on a standalone (non-multiplex) gateway running a
    named profile, _authorization_adapter() used to treat the active name as a
    multiplex secondary, find no _profile_adapters entry, fail closed, and
    rewind the claim forever — silent zero-delivery.
    """
    db_path = tmp_path / "actionable-block.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    reason = "AGE-39 — https://linear.example/AGE-39 — publishing verified."
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="approval", assignee="publisher")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="main",
        )
        kb.block_task(conn, tid, reason=reason, kind="needs_input")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "main"

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    message = adapter.sent[0]["text"]
    assert tid in message
    assert "blocked" in message


def test_multiplex_named_profile_notifier_uses_profile_bot_for_ping_and_wake_final(
    tmp_path, monkeypatch,
):
    """notify+wake must not fall back to the primary Telegram bot.

    This exercises the real notifier -> deliver_wake -> BasePlatformAdapter
    background final-send path. The active profile is deliberately reported as
    ``ang`` to match a background task inheriting Ang's profile runtime scope;
    in multiplex mode the Ang profile adapter still owns the Telegram bot.
    """
    db_path = tmp_path / "ang-notify-wake.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="ang-owned completion",
            assignee="worker",
            session_id="agent:ang:telegram:dm:shared-dm",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="shared-dm",
            chat_type="dm",
            notifier_profile="ang",
            delivery_mode="notify+wake",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    default_adapter = BotIdAdapter("default-bot")
    ang_adapter = BotIdAdapter("ang-bot")
    runner = _make_real_path_runner(default_adapter, ang_adapter, tmp_path)

    asyncio.run(
        _run_one_notifier_tick_and_drain(
            monkeypatch, runner, default_adapter, ang_adapter
        )
    )

    assert default_adapter.sent == []
    ang_contents = [item["content"] for item in ang_adapter.sent]
    assert any(tid in content for content in ang_contents)
    assert "agent final from ang" in ang_contents
    assert {item["bot_id"] for item in ang_adapter.sent} == {"ang-bot"}


def test_non_dispatch_gateway_claims_only_its_profile_subscriptions(
    tmp_path, monkeypatch,
):
    """A profile gateway delivers its events while another gateway dispatches."""
    db_path = tmp_path / "cross-profile-notifier.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        foreign_tid = kb.create_task(
            conn, title="default-owned", assignee="worker",
        )
        kb.add_notify_sub(
            conn,
            task_id=foreign_tid,
            platform="telegram",
            chat_id="default-chat",
            notifier_profile="default",
        )
        kb.complete_task(conn, foreign_tid, summary="default done")

        owned_tid = kb.create_task(
            conn, title="writer-owned", assignee="worker",
        )
        kb.add_notify_sub(
            conn,
            task_id=owned_tid,
            platform="telegram",
            chat_id="writer-chat",
            notifier_profile="writer",
        )
        kb.complete_task(conn, owned_tid, summary="writer done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "writer"
    runner._kanban_dispatcher_lock_handle = None

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [delivery["chat_id"] for delivery in adapter.sent] == ["writer-chat"]
    assert owned_tid in adapter.sent[0]["text"]
    assert len(_unseen_terminal_events_for(foreign_tid, "default-chat")) == 1


def test_legacy_subscription_requires_confirmed_dispatcher_lock_owner(
    tmp_path, monkeypatch,
):
    """Startup and lock-losing gateways cannot claim legacy notifications."""
    db_path = tmp_path / "legacy-lock-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="legacy", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="legacy-chat",
        )
        kb.complete_task(conn, task_id, summary="legacy done")
    finally:
        conn.close()

    startup_adapter = RecordingAdapter()
    startup_runner = _make_runner(startup_adapter)
    startup_runner._kanban_dispatcher_lock_handle = None
    asyncio.run(_run_one_notifier_tick(monkeypatch, startup_runner))
    assert startup_adapter.sent == []
    assert len(_unseen_terminal_events_for(task_id, "legacy-chat")) == 1

    lock_path = tmp_path / ".dispatcher.lock"
    winner_handle, winner_state = _acquire_singleton_lock(lock_path)
    loser_handle, loser_state = _acquire_singleton_lock(lock_path)
    try:
        assert winner_state == "held"
        assert loser_state == "contended"

        loser_adapter = RecordingAdapter()
        loser_runner = _make_runner(loser_adapter)
        loser_runner._kanban_dispatcher_lock_handle = loser_handle
        asyncio.run(_run_one_notifier_tick(monkeypatch, loser_runner))
        assert loser_adapter.sent == []
        assert len(_unseen_terminal_events_for(task_id, "legacy-chat")) == 1

        winner_adapter = RecordingAdapter()
        winner_runner = _make_runner(winner_adapter)
        winner_runner._kanban_dispatcher_lock_handle = winner_handle
        asyncio.run(_run_one_notifier_tick(monkeypatch, winner_runner))
        assert [item["chat_id"] for item in winner_adapter.sent] == ["legacy-chat"]
        assert task_id in winner_adapter.sent[0]["text"]
    finally:
        _release_singleton_lock(loser_handle)
        _release_singleton_lock(winner_handle)


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


class ReportedFailureAdapter:
    """Adapter that REPORTS failure via SendResult(success=False) instead of
    raising — the exact contract the Telegram adapter uses for 'Not connected'
    and degraded-send paths."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        from gateway.platforms.base import SendResult
        return SendResult(success=False, error="Not connected")


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


def test_notifier_subscription_survives_done_reopen_until_archive(
    tmp_path, monkeypatch,
):
    """Done is reversible; archive alone ends notification ownership."""
    db_path = tmp_path / "done-reopen-archive.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="review continuation",
            assignee="worker",
            session_id="origin-session",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="origin-chat",
            thread_id="origin-thread",
            user_id="origin-user",
            chat_type="group",
            notifier_profile="reviewer",
            delivery_mode="notify+wake",
        )
        assert kb.complete_task(conn, tid, summary="first completion")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert adapter.sent[0]["chat_id"] == "origin-chat"
    assert adapter.sent[0]["metadata"]["thread_id"] == "origin-thread"
    assert adapter.handled[0].source.thread_id == "origin-thread"
    assert adapter.handled[0].source.profile == "reviewer"

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, "completion must retain the origin subscription"
        first_cursor = subs[0]["last_event_id"]
    finally:
        conn.close()

    # A quiet tick proves the completed event cannot replay after its cursor
    # was advanced, even though the subscription now remains present.
    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            kb._append_event(conn, tid, "status", {"status": "ready"})
        assert kb.complete_task(conn, tid, summary="corrected completion")
    finally:
        conn.close()

    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The reopen status and second completion each deliver once, while only
    # completion wakes the exact original session/thread.
    assert len(adapter.sent) == 3
    assert len(adapter.handled) == 2
    assert all(item["chat_id"] == "origin-chat" for item in adapter.sent)
    assert adapter.handled[-1].source.thread_id == "origin-thread"
    assert adapter.handled[-1].source.profile == "reviewer"

    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1
        assert subs[0]["last_event_id"] > first_cursor
        assert kb.archive_task(conn, tid)
    finally:
        conn.close()

    runner = _make_runner(adapter)
    runner._active_profile_name = lambda: "reviewer"
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Archive itself is intentionally silent, but consumes its event and
    # removes the subscription so no later historical event can replay.
    assert len(adapter.sent) == 3
    assert len(adapter.handled) == 2
    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, tid) == []
    finally:
        conn.close()


def test_notifier_wakeup_uses_subscription_chat_type(tmp_path, monkeypatch):
    db_path = tmp_path / "chat-type-wakeup.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="dm requester",
            assignee="worker",
            session_id="origin-session",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-dm",
            chat_type="dm",
            delivery_mode="notify+wake",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
    assert adapter.handled[0].source.chat_type == "dm"

    # The wake must resume the creator's real DM session key — the whole bug
    # was that a hardcoded chat_type="group" made build_session_key() produce
    # a group-scoped key (a NEW session) instead of the ":dm:<chat_id>" shape
    # the original conversation runs under (#56580 / #68874).
    from gateway.session import build_session_key

    wake_key = build_session_key(adapter.handled[0].source)
    assert wake_key == "agent:main:telegram:dm:chat-dm"
    assert ":group:" not in wake_key


def _unseen_terminal_events_for(tid, chat_id):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_isolates_per_subscription_failure(tmp_path, monkeypatch):
    """One bad subscription must not block delivery for all others.

    Regression for #59269: when claim_unseen_events_for_sub raises for one
    subscription, the entire notifier tick used to abort — silently blocking
    delivery for every other subscription.
    """
    db_path = tmp_path / "isolation.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    # Create two tasks with subscriptions and complete both. The BAD task is
    # created first: list_notify_subs() has no ORDER BY, so SQLite's natural
    # scan returns insertion order — the failing subscription must be
    # processed BEFORE the good one or this test passes even without the
    # per-subscription isolation (the good delivery happens before the tick
    # aborts). A deterministic-order shim below removes the reliance on the
    # scan order entirely.
    conn = kb.connect()
    try:
        tid_bad = kb.create_task(conn, title="bad task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid_bad, platform="telegram", chat_id="chat-bad")
        kb.complete_task(conn, tid_bad, summary="done")

        tid_good = kb.create_task(conn, title="good task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid_good, platform="telegram", chat_id="chat-good")
        kb.complete_task(conn, tid_good, summary="done")
    finally:
        conn.close()

    original_claim = kb.claim_unseen_events_for_sub

    def selective_claim(conn, task_id, **kwargs):
        if task_id == tid_bad:
            raise RuntimeError("simulated DB corruption for bad task")
        return original_claim(conn, task_id=task_id, **kwargs)

    monkeypatch.setattr(kb, "claim_unseen_events_for_sub", selective_claim)

    # Force the failing subscription to be iterated FIRST regardless of the
    # unordered SELECT's scan order.
    original_list = kb.list_notify_subs

    def bad_first(conn, task_id=None, **kwargs):
        subs = original_list(conn, task_id, **kwargs)
        return sorted(subs, key=lambda s: 0 if s["task_id"] == tid_bad else 1)

    monkeypatch.setattr(kb, "list_notify_subs", bad_first)

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The good task must still be delivered despite the bad task failing.
    assert len(adapter.sent) == 1
    assert tid_good in adapter.sent[0]["text"]


def test_notifier_delivers_block_loop_detected_triage_ping(tmp_path, monkeypatch):
    """A `block_loop_detected` event must reach the subscriber as a triage ping.

    Regression for the silent-triage gap (PR #62712): kanban_db routes a task
    to `triage` after BLOCK_RECURRENCE_LIMIT re-blocks for the same cause and
    emits ONLY a `block_loop_detected` event — no `blocked`/`status` event.
    Before `block_loop_detected` joined TERMINAL_KINDS with its own message
    branch, that one transition (the whole point of which is to force human
    attention) produced zero notification and the task stalled in triage
    silently.
    """
    db_path = tmp_path / "block-loop.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="loops forever", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(
            conn, tid, "block_loop_detected",
            {"reason": "needs credentials", "kind": "needs_input",
             "recurrences": 2, "limit": kb.BLOCK_RECURRENCE_LIMIT},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, "block_loop_detected must produce a notification"
    text = adapter.sent[0]["text"]
    assert "TRIAGE" in text
    assert tid in text
    assert "needs credentials" in text
    # Cursor advanced: the event is claimed and not re-delivered.
    conn = kb.connect()
    try:
        _, remaining = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            kinds=["block_loop_detected"],
        )
    finally:
        conn.close()
    assert remaining == []


# ---------------------------------------------------------------------------
# Handoffs that hand a decision back to the origin must wake it, not only ping
# it: `review_requested` (implementation done, waiting for a reviewer) and
# `block_loop_detected` (routed to triage) are terminal kinds just like
# `blocked`.
# ---------------------------------------------------------------------------


def _wake_text(adapter):
    """Text of the single synthetic wake turn injected into the adapter."""
    assert len(adapter.handled) == 1, (
        f"expected exactly one wake turn, got {len(adapter.handled)}"
    )
    return getattr(adapter.handled[0], "text", "") or ""


def _review_handoff_task(
    *,
    delivery_mode="notify+wake",
    summary="PR ready: https://example.invalid/pr/7\nfull details below",
):
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="implement the thing",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            chat_type="dm",
            delivery_mode=delivery_mode,
        )
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(
            conn, tid, summary=summary, expected_run_id=run_id,
        ) is True
        return tid
    finally:
        conn.close()


def test_review_requested_wakes_the_origin_session(tmp_path, monkeypatch):
    """A review handoff wakes the origin and carries the worker's summary."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "review-wake.db"))
    kb.init_db()
    tid = _review_handoff_task()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, "the passive review ping is unchanged"
    assert "ready for review" in adapter.sent[0]["text"]

    wake = _wake_text(adapter)
    assert tid in wake
    assert "PR ready: https://example.invalid/pr/7" in wake, (
        "the worker's handoff must ride the wake turn like it does for "
        "`completed`, otherwise the woken reviewer has to re-read the board"
    )


def test_block_loop_detected_wakes_the_origin_session(tmp_path, monkeypatch):
    """A triage escalation wakes the origin so a decision gets made."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "triage-wake.db"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="loops forever",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            chat_type="dm",
            delivery_mode="notify+wake",
        )
        kb._append_event(
            conn, tid, "block_loop_detected",
            {"reason": "needs credentials", "kind": "needs_input",
             "recurrences": 2, "limit": kb.BLOCK_RECURRENCE_LIMIT},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert tid in _wake_text(adapter)


def test_review_requested_does_not_wake_a_notify_only_subscription(
    tmp_path, monkeypatch,
):
    """delivery_mode still decides whether a wake-worthy kind wakes at all."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "review-notify.db"))
    kb.init_db()
    _review_handoff_task(delivery_mode="notify")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.handled == [], (
        "notify-only subscriptions must not be woken by a review handoff"
    )
