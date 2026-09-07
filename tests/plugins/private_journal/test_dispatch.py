"""Real plugin discovery and surface dispatch, fixture profiles only."""
import copy
import json
import queue
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, MagicMock
import pytest


@pytest.fixture
def registered(tmp_path, monkeypatch):
    home = tmp_path / 'profile'
    home.mkdir()
    (home / 'config.yaml').write_text('plugins:\n  enabled: [private-journal]\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    from hermes_cli.plugins import discover_plugins, get_plugin_commands
    discover_plugins(force=True)
    assert get_plugin_commands()['log']['private'] is True
    return home


def raw_records(home):
    return [json.loads(p.read_text()) for p in (home / 'journal' / 'holding').glob('*.json')]


def _role_content(rows):
    return [{"role": row.get("role"), "content": row.get("content")} for row in rows]


def _gateway_source(profile="ops"):
    from gateway.config import Platform
    from gateway.session import SessionSource
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
        profile=profile,
    )


def _gateway_event(text, source):
    from gateway.platforms.base import MessageEvent, MessageType
    return MessageEvent(
        text=text,
        message_type=MessageType.COMMAND if text.startswith("/") else MessageType.TEXT,
        source=source,
        message_id="m1",
        internal=False,
    )


def _wire_gateway_runner(root, profile_home, source, busy=False):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fixture")}
    )
    runner.config.multiplex_profiles = True
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {"ops": {Platform.TELEGRAM: adapter}}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner._running_agents = {"busy": Mock()} if busy else {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._queued_events = {}
    runner._session_db = AsyncMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized_for_source = Mock(return_value=True)
    runner._check_slash_access = Mock(return_value=None)
    runner._resolve_profile_home_for_source = Mock(return_value=profile_home)
    runner._active_profile_name = lambda: "default"
    runner._set_session_env = lambda _context: None
    runner._clear_session_env = lambda _tokens: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._update_prompt_pending = {}
    runner._busy_input_mode = "interrupt"
    runner._draining = False
    runner._external_drain_active = False
    runner._session_run_generation = {}
    runner._session_sources = {}
    runner._pending_native_image_paths_by_session = {}
    runner._background_tasks = {}
    runner._background_task_counter = 0
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._pending_skills_reload_notes = {}
    runner._service_tier = None
    runner._fast_mode_by_session = {}
    runner._goal_state_by_session = {}
    runner._goal_runs_in_progress = set()
    runner._goal_queued_by_session = set()
    runner._default_skills = []
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._recover_telegram_topic_thread_id = lambda source: None
    runner._is_telegram_topic_lane = lambda source: False
    runner._is_telegram_topic_root_lobby = lambda source: False
    runner._should_send_telegram_lobby_reminder = lambda source: False
    runner._read_user_config = lambda: {"approvals": {"destructive_slash_confirm": False}}
    runner._busy_sessions = set()
    runner._is_session_running = lambda key: key in runner._busy_sessions
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._claim_active_session_slot = lambda key, source: (None, None)
    runner._persist_active_agents = lambda: None
    runner._begin_session_run_generation = lambda key: 1
    runner._is_session_run_current = lambda key, generation: True
    runner._release_running_agent_state = lambda key: runner._running_agents.pop(key, None)
    runner._finalize_session_off_loop = AsyncMock()
    runner._run_post_turn_hooks = AsyncMock()
    runner._mark_durable_active_turn = AsyncMock(return_value=True)
    runner._clear_durable_active_turn = AsyncMock(return_value=True)
    runner._restore_moa_one_shot = lambda event, key: None
    runner._restore_pending_one_turn_model_override = lambda key: None
    runner._cache_session_source = lambda key, source: None
    runner._reset_notice_session_info = lambda source: ""
    runner._pinned_session_context_prompt = lambda context, redact, key: "fixture context"
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(
        side_effect=lambda **kwargs: kwargs["event"].text
    )
    runner._voice_channel_sidecar_note = lambda event, source, key: None
    runner._set_pending_turn_sidecar_notes = lambda key, notes: None
    runner._bind_adapter_run_generation = lambda adapter, key, generation: None
    runner._reply_anchor_for_event = lambda event: None
    runner._thread_metadata_for_source = lambda source, *args: {}
    runner._adapter_for_source = lambda source: adapter
    runner._deliver_platform_notice = AsyncMock()
    runner._get_unauthorized_dm_behavior = lambda platform, profile=None: "ignore"
    runner._profile_name_for_source = lambda source: getattr(source, "profile", None)
    source._authorization_profile_home = root
    source.role_authorized = True
    return runner, adapter


def test_real_plugin_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    from hermes_cli.plugins import discover_plugins, get_plugin_commands
    discover_plugins(force=True)
    assert 'log' not in get_plugin_commands()


@pytest.mark.parametrize('busy', [False, True])
def test_cli_editor_private_before_history_and_queues(registered, busy):
    from cli import HermesCLI
    cli = object.__new__(HermesCLI)
    cli._agent_running = busy
    cli.busy_input_mode = 'interrupt'
    cli._pending_input = queue.Queue()
    cli._interrupt_queue = queue.Queue()
    cli._app = None
    cli.agent = Mock()
    cli.conversation_history = [{'role': 'user', 'content': 'ordinary'}]
    before = copy.deepcopy(cli.conversation_history)
    buf = SimpleNamespace(text='/log   unicode —\nline  ', reset=Mock())
    cli._submit_editor_buffer(buf)
    buf.reset.assert_called_once_with(append_to_history=False)
    assert raw_records(registered)[0]['text'] == '  unicode —\nline  '
    assert cli._pending_input.empty() and cli._interrupt_queue.empty()
    assert cli.conversation_history == before
    assert not cli.agent.mock_calls
    buf2 = SimpleNamespace(text='ordinary next', reset=Mock())
    cli.handle_bang_shell = lambda text: False
    cli._agent_running = False
    cli._submit_editor_buffer(buf2)
    assert cli._pending_input.get_nowait() == 'ordinary next'
    assert cli.conversation_history == before


def test_failure_ack_cannot_leak(registered, monkeypatch, caplog, capsys):
    from cli import HermesCLI
    from hermes_cli.plugins import get_plugin_commands
    def fail(raw, **kwargs):
        raise ValueError(raw)
    monkeypatch.setitem(get_plugin_commands()['log'], 'handler', fail)
    cli = object.__new__(HermesCLI)
    assert cli.process_command('/log SECRET_SENTINEL') is True
    assert 'SECRET_SENTINEL' not in caplog.text + capsys.readouterr().out
    assert raw_records(registered) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('authorized,busy', [(True, False), (True, True), (False, True)])
async def test_gateway_authorized_before_session_and_busy(registered, authorized, busy):
    from gateway.run import GatewayRunner
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._is_user_authorized_for_source = Mock(return_value=authorized)
    runner._check_slash_access = Mock(return_value=None)
    runner._resolve_profile_home_for_source = Mock(return_value=registered)
    runner._startup_restore_in_progress = True
    runner._queue_startup_restore_event = Mock(side_effect=AssertionError('queue'))
    runner.session_store = Mock()
    runner._running_agents = {'x': Mock()} if busy else {}
    runner._handle_message_with_agent = AsyncMock(side_effect=AssertionError('agent'))
    event = MessageEvent(text='/log  gateway\n  ', message_type=MessageType.TEXT,
                         source=SessionSource(platform=Platform.TELEGRAM, chat_id='c', user_id='u'))
    response = await runner._handle_message(event)
    assert bool(response) == authorized
    assert len(raw_records(registered)) == int(authorized)
    assert not runner.session_store.mock_calls
    runner._handle_message_with_agent.assert_not_called()
    runner._queue_startup_restore_event.assert_not_called()
    if authorized:
        assert raw_records(registered)[0]['text'] == ' gateway\n  '


@pytest.mark.asyncio
@pytest.mark.parametrize('busy', [False, True])
async def test_gateway_private_capture_then_ordinary_turn_keeps_model_history_clean(
    tmp_path, monkeypatch, busy
):
    import hermes_state
    from gateway.config import Platform
    from gateway.run import _profile_runtime_scope
    from gateway.session import SessionStore
    from hermes_constants import get_hermes_home
    from hermes_cli.plugins import discover_plugins

    root = tmp_path / ".hermes"
    ops = root / "profiles" / "ops"
    ops.mkdir(parents=True)
    (root / "config.yaml").write_text("plugins:\n  enabled: []\n")
    (ops / "config.yaml").write_text("plugins:\n  enabled: [private-journal]\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    with _profile_runtime_scope(ops):
        discover_plugins(force=True)

    source = _gateway_source("ops")
    runner, _adapter = _wire_gateway_runner(root, ops, source, busy=busy)
    runner.session_store = SessionStore(root / "sessions", runner.config)
    store = runner.session_store
    key = runner._session_key_for_source(source)
    cached_agent = SimpleNamespace(session_id="not-used", _session_messages=[
        {"role": "user", "content": "ops old history"},
    ])
    runner._agent_cache[key] = (cached_agent, datetime.now(), 1)
    cache_before = copy.deepcopy(runner._agent_cache)
    observations = []

    class AgentReached(BaseException):
        pass

    async def fake_run_agent(**kwargs):
        observations.append((Path(get_hermes_home()), kwargs))
        raise AgentReached()

    runner._run_agent = fake_run_agent
    root_source = _gateway_source(profile="")
    root_source.profile = None
    root_entry = store.get_or_create_session(root_source)
    root_sid = root_entry.session_id
    store.append_to_transcript(root_sid, {"role": "user", "content": "default old history"})
    with _profile_runtime_scope(ops):
        old_entry = store.get_or_create_session(source)
        old_sid = old_entry.session_id
        store.append_to_transcript(old_sid, {"role": "user", "content": "ops old history"})

    sentinel = "PRIVATE_JOURNAL_SENTINEL"
    try:
        response = await runner._handle_message(_gateway_event(f"/log {sentinel}", source))
        assert response
        assert raw_records(ops)[0]["text"] == sentinel
        assert observations == []
        runner._is_user_authorized_for_source.assert_called_once_with(source)
        runner._check_slash_access.assert_called_once_with(source, "log")
        runner._resolve_profile_home_for_source.assert_called_once_with(source)
        assert runner._agent_cache == cache_before
        with _profile_runtime_scope(ops):
            assert _role_content(store.load_transcript(old_sid)) == [
                {"role": "user", "content": "ops old history"}
            ]
        assert _role_content(store.load_transcript(root_sid)) == [
            {"role": "user", "content": "default old history"}
        ]
        assert raw_records(root) == []

        runner._running_agents.clear()
        with pytest.raises(AgentReached):
            with _profile_runtime_scope(ops):
                await runner._handle_message(_gateway_event("ordinary next", source))

        assert len(observations) == 1
        home, turn = observations[0]
        assert home == ops
        assert turn["message"] == "ordinary next"
        assert turn["source"] is source
        assert turn["session_id"] == old_sid
        assert turn["session_key"] == key
        assert _role_content(turn["history"]) == [{"role": "user", "content": "ops old history"}]
        assert sentinel not in json.dumps(turn["history"])
        assert sentinel not in str(turn["message"])
        with _profile_runtime_scope(ops):
            assert sentinel not in json.dumps(store.load_transcript(old_sid))
        assert sentinel not in json.dumps(store.load_transcript(root_sid))
        for db_path in root.rglob("*.db"):
            assert sentinel.encode() not in db_path.read_bytes()
        assert store._db.get_session(old_sid) is None
        with _profile_runtime_scope(ops):
            assert store._db.get_session(old_sid) is not None
        assert Path(get_hermes_home()) == root
    finally:
        store.close_all_db_handles()


@pytest.mark.parametrize('method,params', [
    ('slash.exec', {'command': 'log   rpc\n  '}),
    ('command.dispatch', {'name': 'log', 'arg': '  rpc\n  '}),
    ('prompt.submit', {'text': '/log   rpc\n  '}),
    ('command.private', {'command': '/log   rpc\n  '}),
])
def test_tui_rpc_no_session_created(registered, method, params):
    from tui_gateway import server
    before = dict(server._sessions)
    result = server.handle_request({'jsonrpc': '2.0', 'id': 'r', 'method': method, 'params': params})
    assert result['result']['type'] == 'plugin'
    assert raw_records(registered)[0]['text'] == '  rpc\n  '
    assert server._sessions == before


def test_profile_scoped_registration_and_raw_isolation(registered, tmp_path, monkeypatch):
    from hermes_cli.private_commands import match_private_command, invoke_private_command
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'config.yaml').write_text('plugins:\n  enabled: [private-journal]\n')
    match = match_private_command('/log other', home=other)
    assert match
    invoke_private_command(match, home=other)
    assert not raw_records(registered)
    assert raw_records(other)[0]['text'] == 'other'
    disabled = tmp_path / 'disabled'
    disabled.mkdir()
    assert match_private_command('/log secret', home=disabled) is None


def test_exact_bang_log_uses_existing_shell_path(registered, monkeypatch):
    from cli import HermesCLI
    import hermes_cli.bang_shell as bang
    cli = object.__new__(HermesCLI)
    cli.config = {}
    cli.agent = None
    cli.session_id = "fixture"
    cli._app = None
    cli.conversation_history = [{'role': 'user', 'content': 'ordinary'}]
    cli.console = Mock()
    cli._attached_images = []
    run = Mock(return_value=0)
    monkeypatch.setattr(bang, 'run_bang_command', run)
    monkeypatch.setattr('tools.terminal_tool._check_all_guards', lambda *a, **kw: {'approved': True})
    before = copy.deepcopy(cli.conversation_history)
    assert cli.handle_bang_shell('!log') is True
    assert run.call_args.args[0] == 'log'
    assert cli.conversation_history == before
    assert raw_records(registered) == []


@pytest.mark.asyncio
async def test_base_adapter_dispatches_private_without_background_lifecycle(registered):
    from gateway.config import PlatformConfig, Platform
    from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
    from gateway.session import SessionSource, build_session_key
    # Use a concrete existing adapter; only the outbound transport is faked.
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from hermes_cli.private_commands import match_private_command, invoke_private_command
    adapter = object.__new__(TelegramAdapter)
    adapter.config = PlatformConfig(enabled=True, extra={})
    adapter.platform = Platform.TELEGRAM
    adapter._active_sessions = {}
    adapter._session_key_profile = lambda source: None
    adapter._send_with_retry = AsyncMock()
    async def handler(event):
        return invoke_private_command(match_private_command(event.text), home=registered)
    adapter._message_handler = handler
    adapter._process_message_background = AsyncMock(side_effect=AssertionError('session'))
    event = MessageEvent(text='/log   adapter\n ', message_type=MessageType.COMMAND,
                         source=SessionSource(platform=Platform.TELEGRAM, chat_id='c', user_id='u'))
    key = build_session_key(event.source)
    adapter._active_sessions[key] = object()
    await BasePlatformAdapter.handle_message(adapter, event)
    assert raw_records(registered)[0]['text'] == '  adapter\n '
    adapter._process_message_background.assert_not_called()
    assert key in adapter._active_sessions
    adapter._send_with_retry.assert_awaited_once()
