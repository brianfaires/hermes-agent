"""Real SessionDB-backed regression coverage for gateway /compress.

The mocked tests in ``test_compress_command.py`` set the temp agent's
``session_id`` equal to ``session_entry.session_id``, so the handler's
``rotated`` flag is False in every one of them — accidentally matching the
production no-op these tests exist to prevent. Nothing there pins either
branch of the rotation guard, and nothing uses a real SessionDB.

These tests drive a real ``SessionStore``/``SessionDB`` so both branches are
pinned: rotation succeeds and persists into a child session without
duplicating the parent, and rotation failure preserves the parent intact.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionEntry, build_session_key

from tests.gateway.test_compress_command import (
    _make_event,
    _make_history,
    _make_runner,
    _make_source,
)


class _FakeCompressor:
    """Stand-in for ContextCompressor — stubs only the aux-LLM call."""

    def __init__(self, compressed):
        self._compressed = compressed
        self.compression_count = 1
        self._last_compress_aborted = False
        self._last_summary_error = None
        self._last_summary_dropped_count = 0
        self._last_aux_model_failure_model = None
        self._last_aux_model_failure_error = None
        self.last_compression_rough_tokens = 0
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0

    def has_content_to_compress(self, messages):
        return True

    def compress(self, messages, current_tokens=None, focus_topic=None, force=False):
        return list(self._compressed)


def _make_real_runner(tmp_path, monkeypatch, history):
    """GatewayRunner backed by a real SessionStore over a tmp SQLite DB.

    Pinning ``DEFAULT_DB_PATH`` is mandatory: it is a module-level constant
    resolved at ``hermes_state`` import time, before the autouse HERMES_HOME
    fixture fires, so without this the test writes to the real
    ``~/.hermes/state.db``.
    """
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")

    from gateway.run import GatewayRunner
    from gateway.session import SessionStore

    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    store = SessionStore(sessions_dir=tmp_path, config=config)

    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._session_db = store._db

    sid = "sess-1"
    store._db.create_session(session_id=sid, source="test")
    for i, msg in enumerate(history):
        store.append_to_transcript(sid, {**msg, "timestamp": float(i + 1)})

    entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id=sid,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    store._ensure_loaded()
    store._entries[entry.session_key] = entry
    return runner, store, entry


def _make_rotating_agent(session_db, session_id, compressed):
    """An AIAgent shell whose flush/rotation internals are REAL.

    Only the aux-LLM call and prompt/status plumbing are stubbed. In
    particular ``_compress_context`` and ``_flush_messages_to_session_db``
    are the genuine methods, so a pre-rotation flush that duplicates the
    already-canonical parent transcript is caught here rather than in
    production.
    """
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._session_db = session_db
    agent.session_id = session_id
    agent.model = "test-model"
    agent.platform = "test"
    agent._memory_manager = None
    agent._session_db_created = True
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._session_init_model_config = None
    agent._compression_feasibility_checked = True
    agent._cached_system_prompt = ""
    agent.tools = None
    agent.event_callback = None
    agent.log_prefix = ""
    agent.context_compressor = _FakeCompressor(compressed)

    agent._todo_store = MagicMock()
    agent._todo_store.format_for_injection.return_value = ""
    agent._emit_status = MagicMock()
    agent._emit_warning = MagicMock()
    agent._vprint = MagicMock()
    agent._invalidate_system_prompt = MagicMock()
    agent._build_system_prompt = MagicMock(return_value="sys")
    agent.commit_memory_session = MagicMock()
    agent.shutdown_memory_provider = MagicMock()
    agent.close = MagicMock()
    agent._print_fn = None
    return agent


@pytest.mark.asyncio
async def test_compress_passes_session_db_to_agent():
    """The temp agent MUST receive the gateway's SessionDB.

    Without it, compress_context's rotation block is skipped wholesale and
    the compressed transcript is computed, paid for, then discarded.
    """
    history = _make_history()
    runner = _make_runner(history)
    sentinel = object()
    runner._session_db = sentinel

    agent_instance = MagicMock()
    agent_instance._cached_system_prompt = ""
    agent_instance.tools = None
    agent_instance.context_compressor.has_content_to_compress.return_value = True
    agent_instance.session_id = "sess-1"
    agent_instance._compress_context.return_value = (list(history), "")

    ctor = MagicMock(return_value=agent_instance)
    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "***"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", ctor),
        patch("agent.model_metadata.estimate_request_tokens_rough", return_value=100),
    ):
        await runner._handle_compress_command(_make_event())

    ctor.assert_called_once()
    assert ctor.call_args.kwargs["session_db"] is sentinel


@pytest.mark.asyncio
async def test_compress_rotates_and_persists_against_real_db(tmp_path, monkeypatch):
    """Success path: the compressed handoff lands in a NEW child session and
    the parent transcript survives verbatim — exactly once, not duplicated."""
    history = _make_history()
    compressed = [{"role": "user", "content": "summary of the conversation"}]
    runner, store, entry = _make_real_runner(tmp_path, monkeypatch, history)

    agent = _make_rotating_agent(runner._session_db, "sess-1", compressed)

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "***"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent),
    ):
        result = await runner._handle_compress_command(_make_event())

    # Rotation happened and the gateway followed it onto the child session.
    new_sid = agent.session_id
    assert new_sid != "sess-1"
    assert entry.session_id == new_sid

    # The compressed handoff is persisted into the NEW child session.
    new_transcript = store.load_transcript(new_sid)
    assert len(new_transcript) == 1
    assert new_transcript[0]["content"] == "summary of the conversation"

    # The parent transcript is preserved EXACTLY ONCE. This is the assertion
    # that catches a pre-rotation flush re-inserting messages that were
    # already canonical in SQLite (they came from load_transcript).
    parent = store.load_transcript("sess-1")
    assert [m["content"] for m in parent] == ["one", "two", "three", "four"]

    # Lineage is recorded so the parent stays discoverable.
    assert runner._session_db.get_session(new_sid)["parent_session_id"] == "sess-1"

    assert "Compressed:" in result


@pytest.mark.asyncio
async def test_compress_preserves_transcript_when_rotation_fails(tmp_path, monkeypatch):
    """Failure path: no SessionDB means no rotation. The parent transcript
    must not be rewritten, success must not be claimed, and prompt-token
    accounting must not be reset."""
    history = _make_history()
    runner, store, entry = _make_real_runner(tmp_path, monkeypatch, history)
    runner._session_db = None

    store.rewrite_transcript = MagicMock(wraps=store.rewrite_transcript)
    store.update_session = MagicMock(wraps=store.update_session)

    compressed = [{"role": "user", "content": "summary that must NOT be persisted"}]
    agent = _make_rotating_agent(None, "sess-1", compressed)

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "***"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent),
    ):
        result = await runner._handle_compress_command(_make_event())

    # No rotation, so the parent transcript must be left exactly as it was.
    assert agent.session_id == "sess-1"
    assert entry.session_id == "sess-1"
    store.rewrite_transcript.assert_not_called()

    parent = store.load_transcript("sess-1")
    assert [m["content"] for m in parent] == ["one", "two", "three", "four"]

    # The user must not be told compression succeeded.
    assert "Compressed:" not in result

    # Prompt-token accounting must not be reset when nothing was persisted.
    for call in store.update_session.call_args_list:
        assert call.kwargs.get("last_prompt_tokens") != 0


@pytest.mark.asyncio
async def test_compress_preserves_parent_when_child_creation_fails(tmp_path, monkeypatch):
    """compress_context() assigns the child id BEFORE creating the child row,
    and swallows any failure in between. A changed ``session_id`` therefore
    does NOT prove the child exists — the gateway must not follow the agent
    onto a session that was never created."""
    history = _make_history()
    runner, store, entry = _make_real_runner(tmp_path, monkeypatch, history)
    db = runner._session_db
    monkeypatch.setattr(
        db, "create_session", MagicMock(side_effect=RuntimeError("disk I/O error"))
    )
    store.rewrite_transcript = MagicMock(wraps=store.rewrite_transcript)
    store.update_session = MagicMock(wraps=store.update_session)

    compressed = [{"role": "user", "content": "summary that must NOT be persisted"}]
    agent = _make_rotating_agent(db, "sess-1", compressed)

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "***"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent),
    ):
        result = await runner._handle_compress_command(_make_event())

    # The agent's session_id moved to a child it never managed to create.
    assert agent.session_id != "sess-1"
    assert db.get_session(agent.session_id) is None

    # The gateway must stay on the parent, which still holds the transcript.
    assert entry.session_id == "sess-1"
    store.rewrite_transcript.assert_not_called()
    parent = store.load_transcript("sess-1")
    assert [m["content"] for m in parent] == ["one", "two", "three", "four"]
    assert db.get_session("sess-1")["ended_at"] is None

    # The user must not be told compression succeeded.
    assert "Compressed:" not in result

    # Prompt-token accounting must not be reset when nothing was persisted.
    for call in store.update_session.call_args_list:
        assert call.kwargs.get("last_prompt_tokens") != 0


@pytest.mark.asyncio
async def test_compress_preserves_parent_when_transcript_write_fails(tmp_path, monkeypatch):
    """Rotation can succeed while the compressed transcript still fails to
    land — ``rewrite_transcript`` swallows the DB error. Moving the gateway
    onto that child would strand the user on an EMPTY session while the real
    history sits on the parent."""
    history = _make_history()
    runner, store, entry = _make_real_runner(tmp_path, monkeypatch, history)
    db = runner._session_db
    monkeypatch.setattr(
        db, "replace_messages", MagicMock(side_effect=RuntimeError("database is locked"))
    )
    store.update_session = MagicMock(wraps=store.update_session)

    compressed = [{"role": "user", "content": "summary that must NOT be persisted"}]
    agent = _make_rotating_agent(db, "sess-1", compressed)

    with (
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "***"}),
        patch("gateway.run._resolve_gateway_model", return_value="test-model"),
        patch("run_agent.AIAgent", return_value=agent),
    ):
        result = await runner._handle_compress_command(_make_event())

    # Rotation itself created a child, but the compressed write failed. The
    # failed split must be rolled back atomically: leaving an empty child
    # behind makes it an unreachable/resume-hijacking orphan.
    new_sid = agent.session_id
    assert new_sid != "sess-1"
    assert db.get_session(new_sid) is None

    # The gateway must NOT hand the user an empty session.
    assert entry.session_id == "sess-1"
    parent = store.load_transcript("sess-1")
    assert [m["content"] for m in parent] == ["one", "two", "three", "four"]
    assert db.get_session("sess-1")["ended_at"] is None

    # Preserving the parent means preserving how it RESOLVES, not just its
    # rows. compress_context() already ended it with end_reason='compression',
    # so the lineage walk treats the empty child as the live continuation and
    # would strand a later /resume on it. A parent that was not, in fact,
    # compressed must not be left advertising a continuation.
    assert db.resolve_resume_session_id("sess-1") == "sess-1"
    assert db.get_session(new_sid) is None

    # The user must not be told compression succeeded.
    assert "Compressed:" not in result

    # Prompt-token accounting must not be reset when nothing was persisted.
    for call in store.update_session.call_args_list:
        assert call.kwargs.get("last_prompt_tokens") != 0
