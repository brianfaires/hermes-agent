import asyncio
import importlib.util
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as gateway_run
import model_tools
from agent.i18n import t
from gateway.platforms.base import MessageEvent, MessageType
from gateway.restart import DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
from gateway.session import SessionEntry, build_session_context, build_session_key
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _load_gateway_restart_tool():
    plugin_path = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "gateway-restart-tool"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location(
        "gateway_restart_tool_plugin_gateway_test", plugin_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_restart_command_while_busy_requests_drain_without_interrupt(monkeypatch):
    # Ensure INVOCATION_ID is NOT set — systemd sets this in service mode,
    # which changes the restart call signature.
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m1",
    )
    session_key = build_session_key(event.source)
    running_agent = MagicMock()
    runner._running_agents[session_key] = running_agent

    result = await runner._handle_message(event)

    expected = t("gateway.draining", count=1)
    assert result == expected
    # Guard against the silent-degradation regression in #22266: if the i18n
    # catalog cannot be resolved (e.g. xdist workers losing the locales path)
    # then ``t("gateway.draining", count=1)`` returns the bare key
    # ``"gateway.draining"`` instead of the formatted English string, and both
    # sides of the equality above would still match. Assert on the catalog
    # output explicitly so a broken locale resolution fails loudly here.
    assert expected != "gateway.draining"
    assert "Draining" in expected and "1" in expected
    running_agent.interrupt.assert_not_called()
    runner.request_restart.assert_called_once_with(
        detached=True,
        via_service=False,
        defer_until_session_delivered=session_key,
    )


@pytest.mark.asyncio
async def test_gateway_restart_tool_allows_caller_to_finish_before_restart_proceeds(
    monkeypatch, tmp_path
):
    module = _load_gateway_restart_tool()
    runner, _adapter = make_restart_runner()
    runner._running_agents["caller"] = MagicMock()
    stop_calls = []

    async def stop(**kwargs):
        stop_calls.append(
            {"kwargs": kwargs, "active_agents": runner._running_agent_count()}
        )
        runner._shutdown_event.set()

    runner.stop = stop
    monkeypatch.setattr(module, "_plugin_config", lambda: {"cooldown_seconds": 0})
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", lambda _record: None)
    monkeypatch.setattr(module, "_audit_path", lambda: tmp_path / "audit.jsonl")
    monkeypatch.setattr(module, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(module, "_resolve_runner", lambda: runner)
    monkeypatch.setattr(module, "_restart_modes", lambda: (False, True))

    result = json.loads(
        module._handle_request_gateway_restart(
            {"reason": "reload configuration", "confirm": "restart gateway"}
        )
    )

    assert result["ok"] is True
    assert result["status"] == "restart_draining"
    assert result["active_agents"] == 1
    assert runner._restart_task_started is True

    runner._running_agents.clear()
    await asyncio.sleep(result["scheduled_after_seconds"] + 0.1)
    assert runner._restart_task is not None
    await runner._restart_task

    assert runner._restart_requested is True
    assert stop_calls == [
        {
            "kwargs": {
                "restart": True,
                "detached_restart": False,
                "service_restart": True,
            },
            "active_agents": 0,
        }
    ]


@pytest.mark.asyncio
async def test_gateway_restart_tool_drains_immediately_and_waits_for_delivery(
    monkeypatch, tmp_path
):
    module = _load_gateway_restart_tool()
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    session_key = build_session_key(source)
    durable_task_id = "persisted-session-id"
    session_entry = SessionEntry(
        session_key=session_key,
        session_id=durable_task_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=source,
        platform=source.platform,
        chat_type=source.chat_type,
    )
    send_entered = asyncio.Event()
    release_send = asyncio.Event()
    stop_calls = []
    turn_observations = {}

    async def delayed_send(chat_id, content, reply_to=None, metadata=None):
        send_entered.set()
        await release_send.wait()
        return await type(adapter).send(adapter, chat_id, content, reply_to, metadata)

    async def stop(**kwargs):
        stop_calls.append(
            {"kwargs": kwargs, "active_agents": runner._running_agent_count()}
        )
        runner._shutdown_event.set()

    async def run_tool_turn(event, source, quick_key, run_generation):
        context = build_session_context(source, runner.config, session_entry)
        tokens = runner._set_session_env(context)
        try:
            tool_result = model_tools.handle_function_call(
                "request_gateway_restart",
                {"reason": "reload configuration", "confirm": "restart gateway"},
                task_id=durable_task_id,
            )
            repeat_result = model_tools.handle_function_call(
                "request_gateway_restart",
                {"reason": "reload configuration", "confirm": "restart gateway"},
                task_id=durable_task_id,
            )
        finally:
            runner._clear_session_env(tokens)
        followup = await runner._handle_message(
            MessageEvent(
                text="new turn",
                message_type=MessageType.TEXT,
                source=make_restart_source("fresh"),
                message_id="m-followup",
            )
        )
        turn_observations["tool_result"] = tool_result
        turn_observations["repeat_result"] = repeat_result
        turn_observations["followup"] = followup
        return "restart accepted"

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(module, "_plugin_config", lambda: {"cooldown_seconds": 60})
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", lambda _record: None)
    monkeypatch.setattr(module, "_audit_path", lambda: tmp_path / "audit.jsonl")
    monkeypatch.setattr(module, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(module, "_resolve_runner", lambda: runner)
    monkeypatch.setattr(module, "_restart_modes", lambda: (False, True))

    entry = model_tools.registry.get_entry("request_gateway_restart")
    original_handler = entry.handler if entry is not None else None
    model_tools.registry.register(
        name="request_gateway_restart",
        toolset="gateway_restart",
        schema=module.REQUEST_GATEWAY_RESTART_SCHEMA,
        handler=module._handle_request_gateway_restart,
        check_fn=module._check_available,
    )
    entry = model_tools.registry.get_entry("request_gateway_restart")
    assert entry is not None
    entry.handler = module._handle_request_gateway_restart
    try:
        runner.stop = stop
        runner._handle_message_with_agent = run_tool_turn
        runner._send_restart_notification = AsyncMock(return_value=None)
        runner._run_processing_hook = AsyncMock()
        runner._recover_telegram_topic_thread_id = MagicMock(return_value=None)
        runner._is_telegram_topic_root_lobby = MagicMock(return_value=False)
        runner._claim_active_session_slot = MagicMock(return_value=(None, None))
        runner._begin_session_run_generation = gateway_run.GatewayRunner._begin_session_run_generation.__get__(
            runner, gateway_run.GatewayRunner
        )
        runner._is_session_run_current = gateway_run.GatewayRunner._is_session_run_current.__get__(
            runner, gateway_run.GatewayRunner
        )
        runner._bind_adapter_run_generation = gateway_run.GatewayRunner._bind_adapter_run_generation.__get__(
            runner, gateway_run.GatewayRunner
        )
        runner._set_session_env = gateway_run.GatewayRunner._set_session_env.__get__(
            runner, gateway_run.GatewayRunner
        )
        runner._clear_session_env = gateway_run.GatewayRunner._clear_session_env.__get__(
            runner, gateway_run.GatewayRunner
        )
        runner._release_turn_lease = MagicMock(return_value=False)
        runner._restore_moa_one_shot = MagicMock()
        runner._restore_pending_one_turn_model_override = MagicMock()
        runner._session_run_generation = {}
        runner._external_drain_active = False
        runner._async_session_store = MagicMock()
        runner._async_session_store.get_or_create_session = AsyncMock(
            return_value=session_entry
        )
        runner._handle_message = gateway_run.GatewayRunner._handle_message.__get__(
            runner, gateway_run.GatewayRunner
        )
        runner._handle_message_with_agent = run_tool_turn
        adapter._message_handler = runner._handle_message
        adapter.send = delayed_send

        event = MessageEvent(
            text="please restart",
            message_type=MessageType.TEXT,
            source=source,
            message_id="m-tool",
        )

        await adapter.handle_message(event)
        task = adapter._session_tasks[session_key]

        await asyncio.wait_for(send_entered.wait(), timeout=1.0)
        assert runner._draining is True
        assert runner._restart_requested is True
        assert stop_calls == []

        release_send.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert runner._restart_task is not None
        await asyncio.wait_for(runner._restart_task, timeout=1.0)

        assert stop_calls == [
            {
                "kwargs": {
                    "restart": True,
                    "detached_restart": False,
                    "service_restart": True,
                },
                "active_agents": 0,
            }
        ]
        assert json.loads(turn_observations["tool_result"])["status"] == "restart_draining"
        assert json.loads(turn_observations["repeat_result"])["status"] == "already_in_progress"
        assert "not accepting new work" in turn_observations["followup"]
    finally:
        if original_handler is not None:
            entry.handler = original_handler
        else:
            model_tools.registry.deregister("request_gateway_restart")


@pytest.mark.asyncio
async def test_request_restart_defers_until_secondary_profile_delivery():
    runner, primary_adapter = make_restart_runner()
    secondary_adapter = type(primary_adapter)()
    secondary_source = make_restart_source(chat_id="secondary-chat")
    secondary_key = build_session_key(secondary_source, profile="ops")
    active = asyncio.Event()
    active._hermes_run_generation = 7
    secondary_adapter._active_sessions[secondary_key] = active
    runner._profile_adapters = {"ops": {secondary_source.platform: secondary_adapter}}
    stop_calls = []

    async def stop(**kwargs):
        stop_calls.append(kwargs)
        runner._shutdown_event.set()

    runner.stop = stop

    assert runner.request_restart(
        detached=False,
        via_service=True,
        defer_until_session_delivered=secondary_key,
    ) is True

    await asyncio.sleep(0)
    assert primary_adapter._post_delivery_callbacks == {}
    assert runner._restart_task is None
    assert stop_calls == []

    callback = secondary_adapter.pop_post_delivery_callback(
        secondary_key,
        generation=7,
    )
    assert callback is not None
    callback()

    assert runner._restart_task is not None
    await runner._restart_task
    assert stop_calls == [
        {"restart": True, "detached_restart": False, "service_restart": True}
    ]


@pytest.mark.asyncio
async def test_gateway_restart_tool_restart_preserves_active_session_notification(
    monkeypatch, tmp_path
):
    module = _load_gateway_restart_tool()
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._restart_drain_timeout = 0.0
    runner._launch_systemd_restart_shortcut = MagicMock()
    source = make_restart_source(thread_id="42")
    session_key = build_session_key(source)
    running_agent = MagicMock()
    runner._running_agents[session_key] = running_agent
    runner._cache_session_source(session_key, source)
    monkeypatch.setattr(module, "_plugin_config", lambda: {"cooldown_seconds": 0})
    monkeypatch.setattr(module, "_active_profile_name", lambda: "ops")
    monkeypatch.setattr(module, "_append_audit", lambda _record: None)
    monkeypatch.setattr(module, "_audit_path", lambda: tmp_path / "audit.jsonl")
    monkeypatch.setattr(module, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(module, "_resolve_runner", lambda: runner)
    monkeypatch.setattr(module, "_restart_modes", lambda: (False, True))

    with patch("gateway.status.remove_pid_file"), patch(
        "gateway.status.write_runtime_status"
    ), patch("agent.auxiliary_client.shutdown_cached_clients"):
        result = json.loads(
            module._handle_request_gateway_restart(
                {"reason": "reload configuration", "confirm": "restart gateway"}
            )
        )
        await asyncio.sleep(result["scheduled_after_seconds"] + 0.1)
        assert runner._restart_task is not None
        await runner._restart_task

    assert result["status"] == "restart_draining"
    assert running_agent.interrupt.called
    assert adapter.sent_calls
    chat_id, message, metadata = adapter.sent_calls[0]
    assert chat_id == source.chat_id
    assert "Gateway restarting" in message
    assert metadata["thread_id"] == source.thread_id
    assert metadata["direct_messages_topic_id"] == source.thread_id
    assert runner._shutdown_event.is_set() is True


@pytest.mark.asyncio
async def test_drain_queue_mode_queues_follow_up_without_interrupt():
    runner, adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"

    event = MessageEvent(
        text="follow up",
        message_type=MessageType.TEXT,
        source=make_restart_source(),
        message_id="m2",
    )
    session_key = build_session_key(event.source)
    adapter._active_sessions[session_key] = asyncio.Event()

    await adapter.handle_message(event)

    assert session_key in adapter._pending_messages
    assert adapter._pending_messages[session_key].text == "follow up"
    assert not adapter._active_sessions[session_key].is_set()
    assert any("queued for the next turn" in message for message in adapter.sent)


@pytest.mark.asyncio
async def test_draining_rejects_new_session_messages():
    runner, _adapter = make_restart_runner()
    runner._draining = True
    runner._restart_requested = True

    event = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=make_restart_source("fresh"),
        message_id="m3",
    )

    result = await runner._handle_message(event)

    assert result == "⏳ Gateway is restarting and is not accepting new work right now."


def test_load_busy_input_mode_prefers_env_then_config_then_default(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_INPUT_MODE", raising=False)

    assert gateway_run.GatewayRunner._load_busy_input_mode() == "interrupt"

    (tmp_path / "config.yaml").write_text(
        "display:\n  busy_input_mode: queue\n", encoding="utf-8"
    )
    assert gateway_run.GatewayRunner._load_busy_input_mode() == "queue"

    (tmp_path / "config.yaml").write_text(
        "display:\n  busy_input_mode: steer\n", encoding="utf-8"
    )
    assert gateway_run.GatewayRunner._load_busy_input_mode() == "steer"

    monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "interrupt")
    assert gateway_run.GatewayRunner._load_busy_input_mode() == "interrupt"

    monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "steer")
    assert gateway_run.GatewayRunner._load_busy_input_mode() == "steer"

    # Unknown values fall through to the safe default
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_INPUT_MODE", "bogus")
    assert gateway_run.GatewayRunner._load_busy_input_mode() == "interrupt"


def test_load_busy_text_mode_follows_input_mode_and_honors_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_TEXT_MODE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_BUSY_INPUT_MODE", raising=False)

    # No knobs set → follows busy_input_mode, which defaults to interrupt.
    assert gateway_run.GatewayRunner._load_busy_text_mode() == "interrupt"

    # busy_input_mode=queue propagates to text handling (single source of truth).
    (tmp_path / "config.yaml").write_text(
        "display:\n  busy_input_mode: queue\n", encoding="utf-8"
    )
    assert gateway_run.GatewayRunner._load_busy_text_mode() == "queue"

    # Legacy explicit busy_text_mode still wins for backward compat.
    (tmp_path / "config.yaml").write_text(
        "display:\n  busy_input_mode: interrupt\n  busy_text_mode: queue\n",
        encoding="utf-8",
    )
    assert gateway_run.GatewayRunner._load_busy_text_mode() == "queue"

    # Legacy env override wins too.
    (tmp_path / "config.yaml").write_text(
        "display:\n  busy_input_mode: interrupt\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_TEXT_MODE", "queue")
    assert gateway_run.GatewayRunner._load_busy_text_mode() == "queue"

    # Bogus legacy value is ignored → falls through to busy_input_mode (interrupt).
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_TEXT_MODE", "bogus")
    assert gateway_run.GatewayRunner._load_busy_text_mode() == "interrupt"


def test_load_restart_drain_timeout_prefers_env_then_config_then_default(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.delenv("HERMES_RESTART_DRAIN_TIMEOUT", raising=False)

    assert (
        gateway_run.GatewayRunner._load_restart_drain_timeout()
        == DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    )

    (tmp_path / "config.yaml").write_text(
        "agent:\n  restart_drain_timeout: 12\n", encoding="utf-8"
    )
    assert gateway_run.GatewayRunner._load_restart_drain_timeout() == 12.0

    monkeypatch.setenv("HERMES_RESTART_DRAIN_TIMEOUT", "7")
    assert gateway_run.GatewayRunner._load_restart_drain_timeout() == 7.0

    monkeypatch.setenv("HERMES_RESTART_DRAIN_TIMEOUT", "invalid")
    assert (
        gateway_run.GatewayRunner._load_restart_drain_timeout()
        == DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
    )
    assert "Invalid restart_drain_timeout" in caplog.text


@pytest.mark.asyncio
async def test_request_restart_is_idempotent():
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._launch_detached_restart_command = AsyncMock()

    # _run_restart is held on self._restart_task and is intentionally NOT in
    # _background_tasks, so _stop_impl's cancel loop can't abort it mid-await
    # (see #12875).
    assert runner.request_restart(detached=True, via_service=False) is True
    assert runner._restart_task is not None
    assert runner._restart_task not in runner._background_tasks
    assert runner.request_restart(detached=True, via_service=False) is False

    await runner._restart_task

    runner._launch_detached_restart_command.assert_awaited_once_with()
    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=True, service_restart=False
    )


@pytest.mark.asyncio
async def test_run_restart_excluded_from_stop_cancel_loop():
    """Regression for #12875: _run_restart is held on self._restart_task and
    kept OUT of _background_tasks, and the _stop_impl cancel loop explicitly
    skips it. If it were in _background_tasks, the cancel loop (which fires
    while _run_restart is awaiting _stop_task) would propagate CancelledError
    into _stop_impl and skip _shutdown_event.set() / _exit_code = 75."""
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()

    # A decoy background task that SHOULD be cancelled, plus the restart task
    # that must NOT be.
    async def _decoy():
        await asyncio.sleep(60)

    decoy = asyncio.create_task(_decoy())
    runner._background_tasks.add(decoy)
    decoy.add_done_callback(runner._background_tasks.discard)

    assert runner.request_restart(detached=False, via_service=True) is True
    restart_task = runner._restart_task
    assert restart_task is not None
    assert restart_task not in runner._background_tasks

    # Run the real cancel loop body in isolation (mirrors _stop_impl:7234).
    runner._stop_task = None
    for _task in list(runner._background_tasks):
        if _task is runner._stop_task:
            continue
        if _task is runner._restart_task:
            continue
        _task.cancel()

    await asyncio.sleep(0)  # let cancellation settle
    assert decoy.cancelled()
    assert not restart_task.cancelled()

    await restart_task
    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=False, service_restart=True
    )


@pytest.mark.asyncio
async def test_launch_detached_restart_command_uses_setsid(monkeypatch):
    runner, _adapter = make_restart_runner()
    popen_calls = []

    monkeypatch.setattr(gateway_run.sys, "platform", "linux")
    monkeypatch.setattr(gateway_run, "_resolve_hermes_bin", lambda: ["/usr/bin/hermes"])
    monkeypatch.setattr(gateway_run.os, "getpid", lambda: 321)
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/setsid" if cmd == "setsid" else None)

    def fake_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        return MagicMock()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    await runner._launch_detached_restart_command()

    assert len(popen_calls) == 1
    cmd, kwargs = popen_calls[0]
    assert cmd[:2] == ["/usr/bin/setsid", "bash"]
    assert "gateway restart" in cmd[-1]
    assert "kill -0 321" in cmd[-1]
    assert "deadline=$(( $(date +%s) +" in cmd[-1]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    # The watcher must NOT inherit the gateway marker, or the CLI's
    # self-restart loop guard refuses to run `hermes gateway restart`.
    assert kwargs["env"].get("_HERMES_GATEWAY") is None


@pytest.mark.asyncio
async def test_detached_restart_helper_is_idempotent(monkeypatch):
    runner, _adapter = make_restart_runner()
    popen_calls = []

    monkeypatch.setattr(gateway_run, "_resolve_hermes_bin", lambda: ["/usr/bin/hermes"])
    monkeypatch.setattr(gateway_run.os, "getpid", lambda: 321)
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: popen_calls.append((a, k)))

    await runner._launch_detached_restart_command()
    await runner._launch_detached_restart_command()

    assert len(popen_calls) == 1


def test_windows_gateway_venv_imports_add_site_packages(monkeypatch, tmp_path):
    venv_dir = tmp_path / "venv"
    site_packages = venv_dir / "Lib" / "site-packages"
    pth_extra = tmp_path / "pywin32_system32"
    site_packages.mkdir(parents=True)
    pth_extra.mkdir()
    (site_packages / "pywin32.pth").write_text(str(pth_extra), encoding="utf-8")
    project_root = str(gateway_run.Path(gateway_run.__file__).resolve().parent.parent)

    monkeypatch.setattr(gateway_run.sys, "platform", "win32")
    monkeypatch.setattr(gateway_run.sys, "path", ["existing"])
    monkeypatch.setenv("VIRTUAL_ENV", str(venv_dir))
    monkeypatch.setenv("PYTHONPATH", "already-there")

    gateway_run._ensure_windows_gateway_venv_imports()

    assert gateway_run.sys.path[:2] == [project_root, str(site_packages)]
    assert str(pth_extra) in gateway_run.sys.path
    assert gateway_run.os.environ["VIRTUAL_ENV"] == str(venv_dir.resolve())
    pythonpath = gateway_run.os.environ["PYTHONPATH"].split(gateway_run.os.pathsep)
    assert pythonpath[:3] == [project_root, str(site_packages), "already-there"]


@pytest.mark.asyncio
async def test_windows_detached_restart_scrubs_gateway_marker(monkeypatch, tmp_path):
    runner, _adapter = make_restart_runner()
    popen_calls = []
    venv_dir = tmp_path / "venv"
    site_packages = venv_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)

    monkeypatch.setattr(gateway_run.sys, "platform", "win32")
    monkeypatch.setattr(gateway_run, "_resolve_hermes_bin", lambda: ["hermes"])
    monkeypatch.setattr(gateway_run.os, "getpid", lambda: 321)
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("VIRTUAL_ENV", str(venv_dir))

    import hermes_cli._subprocess_compat as subprocess_compat

    monkeypatch.setattr(
        subprocess_compat,
        "windows_detach_popen_kwargs",
        lambda: {},
    )

    def fake_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        return MagicMock()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    await runner._launch_detached_restart_command()

    assert len(popen_calls) == 1
    cmd, kwargs = popen_calls[0]
    assert cmd[-3:] == ["hermes", "gateway", "restart"]
    assert kwargs["env"].get("_HERMES_GATEWAY") is None
    assert kwargs["env"]["VIRTUAL_ENV"] == str(venv_dir)
    assert str(site_packages) in kwargs["env"]["PYTHONPATH"].split(gateway_run.os.pathsep)
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


@pytest.mark.asyncio
async def test_windows_detached_restart_uses_pythonw_for_watcher(monkeypatch, tmp_path):
    runner, _adapter = make_restart_runner()
    popen_calls = []
    venv_dir = tmp_path / "venv"
    site_packages = venv_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)

    monkeypatch.setattr(gateway_run.sys, "platform", "win32")
    monkeypatch.setattr(gateway_run.sys, "executable", r"C:\venv\Scripts\python.exe")
    monkeypatch.setattr(gateway_run, "_resolve_hermes_bin", lambda: ["hermes"])
    monkeypatch.setattr(gateway_run.os, "getpid", lambda: 321)
    monkeypatch.setenv("VIRTUAL_ENV", str(venv_dir))

    import hermes_cli._subprocess_compat as subprocess_compat
    import hermes_cli.gateway_windows as gateway_windows

    monkeypatch.setattr(
        gateway_windows,
        "_resolve_detached_python",
        lambda _python: (r"C:\Python311\pythonw.exe", venv_dir, [str(site_packages)]),
    )
    monkeypatch.setattr(
        subprocess_compat,
        "windows_detach_popen_kwargs",
        lambda: {"creationflags": 0x08000008},
    )

    def fake_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        return MagicMock()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    await runner._launch_detached_restart_command()

    assert len(popen_calls) == 1
    cmd, kwargs = popen_calls[0]
    assert cmd[0] == r"C:\Python311\pythonw.exe"
    assert cmd[-3:] == ["hermes", "gateway", "restart"]
    assert kwargs["creationflags"] == 0x08000008


# ── Shutdown notification tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_notification_sent_to_active_sessions():
    """Active sessions receive a notification when the gateway starts shutting down."""
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="999", chat_type="dm")
    session_key = "agent:main:telegram:dm:999"
    runner._running_agents[session_key] = MagicMock()

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent) == 1
    assert "shutting down" in adapter.sent[0]
    assert "interrupted" in adapter.sent[0]


@pytest.mark.asyncio
async def test_shutdown_notification_says_restarting_when_restart_requested():
    """When _restart_requested is True, the message says 'restarting' and mentions /retry."""
    runner, adapter = make_restart_runner()
    runner._restart_requested = True
    session_key = "agent:main:telegram:dm:999"
    runner._running_agents[session_key] = MagicMock()

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent) == 1
    assert "restarting" in adapter.sent[0]
    assert "resume" in adapter.sent[0]


@pytest.mark.asyncio
async def test_shutdown_notification_deduplicates_per_chat():
    """Multiple sessions in the same chat only get one notification."""
    runner, adapter = make_restart_runner()
    # Two sessions (different users) in the same chat
    runner._running_agents["agent:main:telegram:group:chat1:u1"] = MagicMock()
    runner._running_agents["agent:main:telegram:group:chat1:u2"] = MagicMock()

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_shutdown_notification_skipped_when_no_active_agents():
    """No notification is sent when there are no active agents."""
    runner, adapter = make_restart_runner()

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent) == 0


@pytest.mark.asyncio
async def test_shutdown_notification_ignores_pending_sentinels():
    """Pending sentinels (not-yet-started agents) don't trigger notifications."""
    from gateway.run import _AGENT_PENDING_SENTINEL

    runner, adapter = make_restart_runner()
    runner._running_agents["agent:main:telegram:dm:999"] = _AGENT_PENDING_SENTINEL

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent) == 0


@pytest.mark.asyncio
async def test_shutdown_notification_send_failure_does_not_block():
    """If sending a notification fails, the method still completes."""
    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(side_effect=Exception("network error"))
    session_key = "agent:main:telegram:dm:999"
    runner._running_agents[session_key] = MagicMock()

    # Should not raise
    await runner._notify_active_sessions_of_shutdown()


@pytest.mark.asyncio
async def test_shutdown_notification_suppressed_when_flag_disabled():
    """Active-session ping is muted when gateway_restart_notification=False on the platform."""
    from gateway.config import Platform

    runner, adapter = make_restart_runner()
    runner._restart_requested = True
    runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False
    session_key = "agent:main:telegram:dm:999"
    runner._running_agents[session_key] = MagicMock()

    await runner._notify_active_sessions_of_shutdown()

    assert adapter.sent == []


@pytest.mark.asyncio
async def test_shutdown_notification_home_channel_suppressed_when_flag_disabled():
    """Home-channel ping during shutdown is muted when the flag is False."""
    from gateway.config import HomeChannel, Platform

    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-42",
        name="Ops Home",
    )
    runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False

    await runner._notify_active_sessions_of_shutdown()

    assert adapter.sent == []


@pytest.mark.asyncio
async def test_shutdown_notification_uses_persisted_origin_for_colon_ids():
    """Shutdown notifications should route from persisted origin, not reparsed keys."""
    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock()
    source = make_restart_source(chat_id="!room123:example.org", chat_type="group")
    source.platform = gateway_run.Platform.MATRIX
    session_key = build_session_key(source)
    runner._running_agents[session_key] = MagicMock()
    runner.session_store._entries = {
        session_key: SessionEntry(
            session_key=session_key,
            session_id="sess-1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            origin=source,
            platform=source.platform,
            chat_type=source.chat_type,
        )
    }
    runner.adapters = {gateway_run.Platform.MATRIX: adapter}

    await runner._notify_active_sessions_of_shutdown()

    assert adapter.send.await_count == 1


@pytest.mark.asyncio
async def test_drain_suppress_skips_home_channel_keeps_session_ping(tmp_path, monkeypatch):
    """A suppress_notification drain marker mutes ONLY the home-channel broadcast.

    The per-active-session interrupt ping MUST still fire (it carries the
    "your task was interrupted, message me to resume" hint). This is the core
    drain-notification-suppression contract.
    """
    from gateway.config import HomeChannel, Platform
    import gateway.drain_control as dc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    runner, adapter = make_restart_runner()
    # A home channel distinct from the active session's chat.
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-42",
        name="Ops Home",
    )
    # One active session in a different chat.
    runner._running_agents["agent:main:telegram:dm:999"] = MagicMock()

    # NAS auto-update drain: marker present with suppress_notification=True.
    dc.write_drain_request(principal="nas", suppress_notification=True)

    await runner._notify_active_sessions_of_shutdown()

    # Exactly one send — the active-session ping to chat 999. The home-channel
    # broadcast to home-42 was suppressed.
    assert len(adapter.sent_calls) == 1
    sent_chat_ids = {chat_id for chat_id, _content, _meta in adapter.sent_calls}
    assert "999" in sent_chat_ids
    assert "home-42" not in sent_chat_ids
    assert "shutting down" in adapter.sent[0]


@pytest.mark.asyncio
async def test_drain_without_suppress_flag_still_broadcasts_home_channel(tmp_path, monkeypatch):
    """A drain marker WITHOUT the suppress flag leaves today's behaviour intact.

    Both the active-session ping AND the home-channel broadcast fire — proving
    the suppression is opt-in and operator/legacy drains are unaffected.
    """
    from gateway.config import HomeChannel, Platform
    import gateway.drain_control as dc

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-42",
        name="Ops Home",
    )
    runner._running_agents["agent:main:telegram:dm:999"] = MagicMock()

    # Operator drain: marker present, suppress_notification defaults False.
    dc.write_drain_request(principal="dashboard")

    await runner._notify_active_sessions_of_shutdown()

    sent_chat_ids = {chat_id for chat_id, _content, _meta in adapter.sent_calls}
    # Both targets notified (today's behaviour preserved).
    assert "999" in sent_chat_ids
    assert "home-42" in sent_chat_ids
