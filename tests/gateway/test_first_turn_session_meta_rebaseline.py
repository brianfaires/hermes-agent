"""Regression: first-turn ``session_meta`` row must be re-baselined into the
agent cache's message_count snapshot.

Bug
---
On a fresh gateway conversation the post-turn re-baseline
(``_refresh_agent_cache_message_count``) runs *before* the first-turn
``session_meta`` marker row is appended to the transcript:

    gateway/run.py:
        ... agent run completes ...
        self._refresh_agent_cache_message_count(...)   # snapshot taken HERE
        ...
        if not history:                                # session_meta written LATER
            append_to_transcript({"role": "session_meta", ...})

``append_to_transcript`` (no ``skip_db``) increments the session's
``message_count`` unconditionally (hermes_state.append_message), so the
snapshot ends up exactly +1 below the live on-disk count.

The cross-process coherence guard (#45966) compares the live count against
that snapshot on the *next* inbound message, sees ``live != snapshot``,
mistakes this process's own ``session_meta`` write for a foreign write, and
**rebuilds the cached agent on turn 2 of every fresh conversation** — silently
busting the per-conversation prompt cache the cache exists to protect.

The fix re-baselines AFTER all of this turn's transcript writes (including the
first-turn ``session_meta`` row), so the snapshot matches the live count and
the guard fires only on genuinely foreign writes.

This drives the REAL ``_handle_message_with_agent`` against a REAL SessionDB
(the ``session_meta`` write actually increments ``message_count``) and asserts
the invariant: after a first turn, snapshot == live count → next turn reuses.
"""

import sys
import threading
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionEntry, SessionSource


SESSION_KEY = "agent:main:telegram:group:-1001:12345"
SESSION_ID = "sess-first-turn"


def _bootstrap(monkeypatch, tmp_path, db):
    """GatewayRunner wired to a REAL SessionDB for count reads, mirroring the
    proven #42039 harness but with a live cache + real transcript counter."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    config = GatewayConfig()
    runner = gateway_run.GatewayRunner(config)
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    # REAL SessionDB behind the async facade the gateway holds — the
    # production re-baseline does ``await self._session_db.get_session(...)``,
    # so it must be the AsyncSessionDB wrapper, not the raw sync DB.
    from hermes_state import AsyncSessionDB

    runner._session_db = AsyncSessionDB(db)
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._begin_session_run_generation = lambda _key: 1
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    # Live agent cache (not a MagicMock) so the re-baseline actually rewrites
    # the snapshot tuple in place.
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    # Empty history → triggers the first-turn ``session_meta`` write path.
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.update_session = MagicMock()

    # Mirror the real SessionStore.append_to_transcript: forward non-skip_db
    # writes to the real DB so a ``session_meta`` row genuinely increments
    # message_count, exactly as in production. skip_db=True writes (the agent
    # already persisted them via _flush_messages_to_session_db) are no-ops here.
    def _append(session_id, message, skip_db=False):
        if not skip_db:
            db.append_message(
                session_id=session_id,
                role=message.get("role", "unknown"),
                content=message.get("content"),
            )

    runner.session_store.append_to_transcript = MagicMock(side_effect=_append)

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    return runner


def _event():
    return MessageEvent(
        text="hello world",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            user_id="12345",
        ),
        message_id="msg-1",
    )


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="12345",
    )


def _live_count(db, session_id):
    row = db.get_session(session_id)
    return (row.get("message_count", 0) if row else 0)


class _NativeDecisionAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect=False):
        pass

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, **kwargs):
        return SendResult(success=True, message_id="fixture-reply")

    async def get_chat_info(self, chat_id):
        return {}


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["provider_denied", "success", "failed", "incomplete", "partial", "unmarked"])
async def test_native_decision_receipt_requires_successful_turn(monkeypatch, tmp_path, outcome):
    from types import SimpleNamespace
    from gateway.run import TurnRunner
    from gateway.turn_context import TurnContext
    from gateway.wake import deliver_wake
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "sessions.db")
    db.create_session(session_id=SESSION_ID, source="telegram", model="test-model")
    runner = _bootstrap(monkeypatch, tmp_path, db)
    turn_runner = SimpleNamespace(
        _get_system_prompt_for_channel=lambda *args, **kwargs: None,
        _resolve_session_agent_runtime=Mock(side_effect=RuntimeError("subscription route denied")),
    )
    denied = TurnRunner(turn_runner, TurnContext(
        source=_source(), resolve_display_setting=lambda *args: False,
    )).run_sync()
    assert denied["api_calls"] == 0 and denied["messages"] == []
    result = denied if outcome == "provider_denied" else {
        "final_response": "fixture decision result", "api_calls": 1,
        "messages": [], "failed": outcome == "failed",
        "completed": outcome not in ("incomplete", "unmarked"),
        "partial": outcome == "partial",
    }
    if outcome == "unmarked":
        result.pop("completed")
    runner._run_agent = AsyncMock(return_value=result)
    adapter = _NativeDecisionAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)

    async def handler(event):
        return await runner._handle_message_with_agent(event, event.source, SESSION_KEY, 1)

    adapter._message_handler = handler
    try:
        if outcome == "success":
            await deliver_wake(adapter, text="Kanban scoped decision", source=_source(), require_decision_receipt=True)
        else:
            with pytest.raises(RuntimeError, match="native decision receiver"):
                await deliver_wake(adapter, text="Kanban scoped decision", source=_source(), require_decision_receipt=True)
        runner._run_agent.assert_awaited_once()
    finally:
        await adapter.cancel_background_tasks()
        db.close()


@pytest.mark.asyncio
async def test_first_turn_session_meta_is_captured_by_rebaseline(
    monkeypatch, tmp_path
):
    """After a fresh first turn, the cache snapshot must equal the live
    message_count — including the first-turn ``session_meta`` row.

    WITHOUT the fix the re-baseline snapshots the count *before* the
    session_meta append, leaving the snapshot one short; the cross-process
    guard then rebuilds the cached agent on turn 2 (prompt-cache churn).
    """
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session(SESSION_ID, source="telegram")

    runner = _bootstrap(monkeypatch, tmp_path, db)

    # Cache snapshot taken at agent-BUILD time = count before this turn's
    # writes (a fresh session → 0). This is what the #45966 guard stores.
    build_count = _live_count(db, SESSION_ID)
    agent_obj = object()
    with runner._agent_cache_lock:
        runner._agent_cache[SESSION_KEY] = (agent_obj, "sig", build_count)

    # Stubbed agent run: the gateway's own user/assistant rows are persisted
    # by the agent (skip_db=True downstream); only the session_meta marker is
    # written by the gateway with skip_db=False.
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "Hi there!",
            "messages": [
                {"role": "user", "content": "hello world"},
                {"role": "assistant", "content": "Hi there!"},
            ],
            "tools": [{"name": "noop"}],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    await runner._handle_message_with_agent(_event(), _source(), SESSION_KEY, 1)

    # The first-turn session_meta row was written → live count advanced.
    live = _live_count(db, SESSION_ID)
    assert live == build_count + 1, (
        "first-turn session_meta should increment message_count by exactly 1"
    )

    # THE INVARIANT: the cache snapshot must now equal the live count, so the
    # next turn's cross-process guard reuses the cached agent.
    with runner._agent_cache_lock:
        cached = runner._agent_cache[SESSION_KEY]
    snapshot = cached[2]
    assert snapshot == live, (
        f"cache snapshot {snapshot} != live count {live}: the first-turn "
        f"session_meta write was not re-baselined, so the #45966 guard will "
        f"rebuild the cached agent on turn 2 and bust the prompt cache."
    )
    # And the cached agent instance must be untouched (never rebuilt).
    assert cached[0] is agent_obj


