from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_make_source(),
        message_id="m1",
        internal=True,
    )


def _session_entry() -> SessionEntry:
    return SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _session_entry()
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._queued_events = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._update_prompt_pending = {}
    runner._busy_input_mode = "interrupt"
    runner._draining = False
    runner._session_run_generation = {}
    runner._session_sources = {}
    runner._pending_native_image_paths_by_session = {}
    runner._background_tasks = {}
    runner._background_task_counter = 0
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._service_tier = None
    runner._fast_mode_by_session = {}
    runner._goal_state_by_session = {}
    runner._goal_runs_in_progress = set()
    runner._goal_queued_by_session = set()
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._should_send_telegram_lobby_reminder = lambda _source: False
    runner._check_slash_access = lambda _source, _command: None
    runner._begin_session_run_generation = lambda _key: 1
    runner._release_running_agent_state = lambda key: runner._running_agents.pop(key, None)
    return runner, adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("command_text", ["/queue do this next", "/q do this next"])
async def test_idle_queue_sends_payload_as_next_turn(command_text):
    runner, _adapter = _make_runner()
    captured = {}

    async def fake_handle_message_with_agent(event, source, key, generation):
        captured["text"] = event.text
        captured["command"] = event.get_command()
        captured["source"] = source
        captured["key"] = key
        captured["generation"] = generation
        return {"final_response": "", "messages": []}

    runner._handle_message_with_agent = fake_handle_message_with_agent

    result = await runner._handle_message(_make_event(command_text))

    assert result == {"final_response": "", "messages": []}
    assert captured["text"] == "do this next"
    assert captured["command"] is None
    assert captured["source"] == _make_source()
    assert captured["key"] == build_session_key(_make_source())
    assert captured["generation"] == 1
    assert runner._running_agents == {}


# ---------------------------------------------------------------------------
# /new (<prompt>) — reset into a fresh session and deliver <prompt> as its
# first user message, exactly once (FC-41).
# ---------------------------------------------------------------------------


def _make_new_command_runner(profile: str = ""):
    """A ``_make_runner`` wired far enough to run the real /new reset path."""
    runner, adapter = _make_runner()
    source = _make_source()
    if profile:
        source.profile = profile
    session_key = build_session_key(source)
    entry = _session_entry()

    runner.session_store._entries = {session_key: entry}
    runner.session_store._generate_session_key.return_value = session_key
    runner.session_store.reset_session.return_value = entry
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._session_db = AsyncMock()
    runner._session_db.get_session_title.return_value = None
    runner._is_telegram_topic_lane = lambda _source: False
    runner._reset_notice_session_info = lambda _source: ""
    runner._finalize_session_off_loop = AsyncMock()
    # Confirmation is exercised separately; default these tests to the
    # "already opted out" gate so the reset runs inline.
    runner._read_user_config = lambda: {"approvals": {"destructive_slash_confirm": False}}
    runner._busy_sessions = set()
    runner._is_session_running = lambda key: key in runner._busy_sessions
    return runner, adapter, source, session_key


def _capture_agent_turns(runner) -> list:
    """Record every text that reaches the agent as its own turn.

    The stand-in returns a plain string, matching the real
    ``_handle_message_with_agent`` contract (reply text, or ``None`` when
    the reply was already streamed).
    """
    turns: list = []

    async def _fake_handle_message_with_agent(event, source, key, generation):
        turns.append(event.text)
        return "agent reply"

    runner._handle_message_with_agent = _fake_handle_message_with_agent
    return turns


def _new_event(text: str, source: SessionSource) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id="m1",
        internal=True,
    )


@pytest.mark.asyncio
async def test_idle_new_with_parenthesized_prompt_delivers_exactly_one_first_turn():
    """`/new (prompt)` resets, then sends `prompt` as the new session's first turn."""
    runner, adapter, source, _key = _make_new_command_runner()
    turns = _capture_agent_turns(runner)

    result = await runner._handle_message(_new_event("/new (draft the release notes)", source))

    assert turns == ["draft the release notes"]
    assert result == "agent reply"
    # The reset banner is delivered out-of-band, exactly once, so the
    # handler's return value can be the first turn's reply.
    assert adapter.send.await_count == 1
    assert "✨" in str(adapter.send.await_args.args[1])
    # Prompt mode must not consume the payload as a session title.
    runner._session_db.set_session_title.assert_not_awaited()
    runner.session_store.reset_session.assert_called_once()


@pytest.mark.asyncio
async def test_idle_new_with_name_still_sets_title_and_starts_no_turn():
    """`/new <name>` keeps its title semantics and starts no agent turn."""
    runner, adapter, source, _key = _make_new_command_runner()
    turns = _capture_agent_turns(runner)

    result = await runner._handle_message(_new_event("/new my-experiment", source))

    assert turns == []
    runner._session_db.set_session_title.assert_awaited_once()
    assert runner._session_db.set_session_title.await_args.args[1] == "my-experiment"
    assert "my-experiment" in str(result)
    assert adapter.send.await_count == 0


@pytest.mark.asyncio
async def test_bare_new_resets_without_starting_a_turn():
    runner, adapter, source, _key = _make_new_command_runner()
    turns = _capture_agent_turns(runner)

    result = await runner._handle_message(_new_event("/new", source))

    assert turns == []
    runner._session_db.set_session_title.assert_not_awaited()
    assert adapter.send.await_count == 0
    assert "✨" in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("command_text", ["/new ()", "/new (   )", "/new ("])
async def test_new_with_empty_parentheses_degrades_to_a_bare_reset(command_text):
    """Empty parentheses carry no message, so they behave exactly like `/new`."""
    runner, adapter, source, _key = _make_new_command_runner()
    turns = _capture_agent_turns(runner)

    result = await runner._handle_message(_new_event(command_text, source))

    assert turns == []
    # Nothing is smuggled into the session title either.
    runner._session_db.set_session_title.assert_not_awaited()
    assert adapter.send.await_count == 0
    assert "✨" in str(result)


@pytest.mark.asyncio
async def test_new_with_unclosed_parenthesis_still_delivers_the_prompt():
    """A missing closing paren must not silently truncate the prompt into a title."""
    runner, adapter, source, _key = _make_new_command_runner()
    turns = _capture_agent_turns(runner)

    await runner._handle_message(_new_event("/new (finish the audit", source))

    assert turns == ["finish the audit"]
    runner._session_db.set_session_title.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_new_with_prompt_interrupts_then_delivers_one_turn():
    """Mid-run `/new (prompt)` interrupts, resets, then runs the prompt once."""
    runner, adapter, source, key = _make_new_command_runner()
    runner._busy_sessions.add(key)
    turns = _capture_agent_turns(runner)
    interrupts: list = []

    async def _fake_interrupt(quick_key, src, *, interrupt_reason=None, invalidation_reason=None):
        interrupts.append((quick_key, invalidation_reason))
        runner._busy_sessions.discard(quick_key)

    runner._interrupt_and_clear_session = _fake_interrupt

    result = await runner._handle_message(_new_event("/new (resume the migration)", source))

    assert interrupts == [(key, "new_command")]
    assert turns == ["resume the migration"]
    assert result == "agent reply"
    assert adapter.send.await_count == 1


@pytest.mark.asyncio
async def test_busy_new_without_prompt_keeps_plain_reset_behavior():
    """Mid-run bare `/new` still interrupts and resets with no confirmation."""
    runner, adapter, source, key = _make_new_command_runner()
    runner._busy_sessions.add(key)
    turns = _capture_agent_turns(runner)
    interrupts: list = []

    async def _fake_interrupt(quick_key, src, *, interrupt_reason=None, invalidation_reason=None):
        interrupts.append((quick_key, invalidation_reason))
        runner._busy_sessions.discard(quick_key)

    runner._interrupt_and_clear_session = _fake_interrupt

    result = await runner._handle_message(_new_event("/new", source))

    assert interrupts == [(key, "new_command")]
    assert turns == []
    assert adapter.send.await_count == 0
    assert "✨" in str(result)


@pytest.mark.asyncio
async def test_new_prompt_confirmation_cancel_delivers_nothing():
    """Cancelling the destructive-slash confirm resets nothing and sends no prompt."""
    from tools import slash_confirm

    runner, adapter, source, key = _make_new_command_runner()
    runner._read_user_config = lambda: {"approvals": {"destructive_slash_confirm": True}}
    adapter.send_slash_confirm = AsyncMock(return_value=SimpleNamespace(success=True))
    turns = _capture_agent_turns(runner)
    slash_confirm.clear(key)

    ack = await runner._handle_message(_new_event("/new (delete nothing please)", source))

    assert ack is None  # buttons rendered; no text ack
    assert turns == []
    runner.session_store.reset_session.assert_not_called()

    pending = slash_confirm.get_pending(key)
    assert pending is not None and pending["command"] == "new"
    reply = await slash_confirm.resolve(key, pending["confirm_id"], "cancel")

    assert "cancelled" in reply
    assert turns == []
    runner.session_store.reset_session.assert_not_called()


@pytest.mark.asyncio
async def test_new_prompt_confirmation_approve_delivers_prompt_exactly_once():
    """Approving runs the reset once and delivers the prompt once, even on a re-click."""
    from tools import slash_confirm

    runner, adapter, source, key = _make_new_command_runner()
    runner._read_user_config = lambda: {"approvals": {"destructive_slash_confirm": True}}
    adapter.send_slash_confirm = AsyncMock(return_value=SimpleNamespace(success=True))
    turns = _capture_agent_turns(runner)
    slash_confirm.clear(key)

    await runner._handle_message(_new_event("/new (ship the fix)", source))
    pending = slash_confirm.get_pending(key)
    assert pending is not None

    reply = await slash_confirm.resolve(key, pending["confirm_id"], "once")

    assert turns == ["ship the fix"]
    assert reply == "agent reply"
    runner.session_store.reset_session.assert_called_once()

    # A second (double-click) resolution of the same confirm is a no-op:
    # the prompt must not be delivered twice.
    again = await slash_confirm.resolve(key, pending["confirm_id"], "once")
    assert again is None
    assert turns == ["ship the fix"]
    runner.session_store.reset_session.assert_called_once()


@pytest.mark.asyncio
async def test_new_prompt_routes_to_the_named_profile_adapter_only():
    """The reset banner and prompt turn stay on the source profile's adapter."""
    runner, default_adapter, source, _key = _make_new_command_runner(profile="ops")
    ops_adapter = MagicMock()
    ops_adapter.send = AsyncMock()
    ops_adapter._pending_messages = {}
    runner._profile_adapters = {"ops": {Platform.TELEGRAM: ops_adapter}}
    runner._active_profile_name = lambda: "default"
    turns = _capture_agent_turns(runner)

    await runner._handle_message(_new_event("/new (audit the ops queue)", source))

    assert turns == ["audit the ops queue"]
    assert ops_adapter.send.await_count == 1
    assert default_adapter.send.await_count == 0


# ---------------------------------------------------------------------------
# Argument parsing contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,expected",
    [
        ("", ("", "")),
        ("my-experiment", ("my-experiment", "")),
        ("  spaced name  ", ("spaced name", "")),
        ("(do the thing)", ("", "do the thing")),
        ("(  padded  )", ("", "padded")),
        ("()", ("", "")),
        ("(", ("", "")),
        ("(unclosed prompt", ("", "unclosed prompt")),
        ("(a) and (b)", ("", "a) and (b")),
        ("name (not a prompt)", ("name (not a prompt)", "")),
    ],
)
def test_parse_new_session_args(args, expected):
    from gateway.slash_commands import parse_new_session_args

    assert parse_new_session_args(args) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["/new (nested)", "/help"])
async def test_new_payload_slash_commands_are_literal(payload):
    runner, _, source, _ = _make_new_command_runner()
    turns = _capture_agent_turns(runner)
    runner._handle_help_command = AsyncMock(return_value="help dispatched")
    await runner._handle_message(_new_event(f"/new ({payload})", source))
    assert turns == [payload]
    runner.session_store.reset_session.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [False, True])
@pytest.mark.parametrize("mode", ["idle", "busy", "confirm", "cancel"])
async def test_new_origin_delivery_is_claimed_before_side_effects(concurrent, mode):
    import asyncio
    from tools import slash_confirm

    runner, adapter, source, key = _make_new_command_runner()
    turns = _capture_agent_turns(runner)
    interrupts = []
    if mode == "busy":
        runner._busy_sessions.add(key)

    async def interrupt(quick_key, src, **kwargs):
        interrupts.append(quick_key)
        await asyncio.sleep(0)  # duplicate must be claimed before this await
        runner._busy_sessions.discard(quick_key)

    runner._interrupt_and_clear_session = interrupt
    runner._read_user_config = lambda: {
        "approvals": {"destructive_slash_confirm": mode in {"confirm", "cancel"}}
    }
    adapter.send_slash_confirm = AsyncMock(return_value=SimpleNamespace(success=True))
    slash_confirm.clear(key)
    try:
        async def deliver():
            return await runner._handle_message(_new_event("/new (hello)", source))

        if concurrent:
            await asyncio.gather(deliver(), deliver())
        else:
            await deliver()
            await deliver()
        if mode in {"confirm", "cancel"}:
            pending = slash_confirm.get_pending(key)
            assert adapter.send_slash_confirm.await_count == 1
            await slash_confirm.resolve(
                key, pending["confirm_id"], "cancel" if mode == "cancel" else "once"
            )
            await deliver()  # cancellation/rotation must not release the origin claim
            assert slash_confirm.get_pending(key) is None
        assert turns == ([] if mode == "cancel" else ["hello"])
        assert runner.session_store.reset_session.call_count == (0 if mode == "cancel" else 1)
        assert interrupts == ([key] if mode == "busy" else [])
    finally:
        slash_confirm.clear(key)


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("concurrent", [False, True])
@pytest.mark.parametrize("payload", ["fresh prompt", "/new (nested)", "/help"])
async def test_new_real_profile_store_and_history_boundary(
    tmp_path, monkeypatch, deferred, concurrent, payload
):
    """Execute dispatch/reset/session/history loading; stop at the external agent seam."""
    from pathlib import Path
    import hermes_state
    from hermes_constants import get_hermes_home
    from gateway.run import _profile_runtime_scope
    from gateway.session import SessionStore
    from tools import slash_confirm

    root = tmp_path / ".hermes"
    ops = root / "profiles" / "ops"
    ops.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    runner, adapter, source, key = _make_new_command_runner(profile="ops")
    runner.config.multiplex_profiles = True
    source._authorization_profile_home = root
    source.role_authorized = True
    runner.session_store = SessionStore(root / "sessions", runner.config)
    # Restore production's context-scoped DB property (fixture pins a fake).
    from gateway.run import _SESSION_DB_UNPINNED
    import threading
    runner._session_db_pinned = _SESSION_DB_UNPINNED
    runner._session_db_handles = {}
    runner._session_db_handles_lock = threading.Lock()
    key = runner._session_key_for_source(source)
    runner._recover_telegram_topic_thread_id = lambda source: None
    runner._read_user_config = lambda: {"approvals": {"destructive_slash_confirm": deferred}}
    adapter.send_slash_confirm = AsyncMock(return_value=SimpleNamespace(success=True))
    runner._profile_adapters = {"ops": {Platform.TELEGRAM: adapter}}
    runner._active_profile_name = lambda: "default"
    runner._default_skills = []
    observations = []

    class AgentReached(BaseException):
        pass

    async def fake_agent(**kwargs):
        observations.append((Path(get_hermes_home()), kwargs))
        raise AgentReached()  # no model/provider call or post-agent streaming

    runner._run_agent = fake_agent
    store = runner.session_store
    root_source = _make_source()
    root_entry = store.get_or_create_session(root_source)
    root_sid = root_entry.session_id
    store.append_to_transcript(root_sid, {"role": "user", "content": "root private history"})
    with _profile_runtime_scope(ops):
        old = store.get_or_create_session(source)
        old_sid = old.session_id
        store.append_to_transcript(old_sid, {"role": "user", "content": "ops old history"})
    slash_confirm.clear(key)
    try:
        import asyncio

        async def deliver():
            with _profile_runtime_scope(ops):
                try:
                    await runner._handle_message(_new_event(f"/new ({payload})", source))
                except AgentReached:
                    pass

        if concurrent:
            await asyncio.gather(deliver(), deliver())
        else:
            await deliver()
            await deliver()
        if deferred:
            assert Path(get_hermes_home()) == root
            pending = slash_confirm.get_pending(key)
            assert pending is not None
            assert adapter.send_slash_confirm.await_count == 1
            with pytest.raises(AgentReached):
                await slash_confirm.resolve(key, pending["confirm_id"], "once")
            await deliver()
            assert slash_confirm.get_pending(key) is None
        assert len(observations) == 1
        home, turn = observations[0]
        assert home == ops
        assert turn["message"] == payload
        assert turn["source"] is source
        assert source._authorization_profile_home == root
        assert source.role_authorized and not source.delivered_via_upstream_relay
        assert turn["history"] == []
        new_sid = turn["session_id"]
        assert new_sid not in {old_sid, root_sid}
        with _profile_runtime_scope(ops):
            assert store._db.get_session(new_sid) is not None
            assert store.load_transcript(old_sid)[0]["content"] == "ops old history"
        assert store._db.get_session(new_sid) is None
        assert store.load_transcript(root_sid)[0]["content"] == "root private history"
        assert Path(get_hermes_home()) == root
    finally:
        slash_confirm.clear(key)
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", ["profile", "platform", "chat_id", "thread_id", "user_id", "scope_id", "home"])
async def test_new_delivery_identity_keeps_source_scopes_distinct(tmp_path, dimension):
    import dataclasses
    from gateway.run import _profile_runtime_scope

    runner, adapter, source, _ = _make_new_command_runner()
    turns = _capture_agent_turns(runner)
    runner._adapter_for_source = lambda source: adapter
    await runner._handle_message(_new_event("/new (hello)", source))
    changes = {dimension: Platform.DISCORD if dimension == "platform" else "other"}
    other_source = source if dimension == "home" else dataclasses.replace(source, **changes)
    if dimension == "home":
        with _profile_runtime_scope(tmp_path):
            await runner._handle_message(_new_event("/new (hello)", other_source))
    else:
        await runner._handle_message(_new_event("/new (hello)", other_source))
    assert turns == ["hello", "hello"]
    assert runner.session_store.reset_session.call_count == 2


def test_new_origin_claim_uses_bounded_existing_cache(monkeypatch):
    from gateway.platforms.helpers import MessageDeduplicator

    runner, _, source, _ = _make_new_command_runner()
    runner._new_command_dedup = MessageDeduplicator(max_size=2, ttl_seconds=10)
    now = [100.0]
    monkeypatch.setattr("gateway.platforms.helpers.time.time", lambda: now[0])
    first = _new_event("/new (hello)", source)
    assert runner._claim_new_command_event(first)
    assert not runner._claim_new_command_event(first)
    for message_id in ["m2", "m3"]:
        event = _new_event("/new (hello)", source)
        event.message_id = message_id
        assert runner._claim_new_command_event(event)
        now[0] += 1
    assert len(runner._new_command_dedup._seen) <= 2
    assert runner._claim_new_command_event(first)  # oldest evicted
    now[0] += 11
    assert runner._claim_new_command_event(first)  # expired
    first.message_id = None
    assert runner._claim_new_command_event(first)
    assert runner._claim_new_command_event(first)  # no invented content identity
