"""A discovered plugin can correlate a reset and fence its queued follow-up turn."""
import asyncio
import json
from queue import SimpleQueue
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig
from gateway.run import _profile_runtime_scope
from hermes_constants import get_hermes_home
from hermes_cli.plugins import get_plugin_manager
from tests.gateway.test_plugin_message_injection import _entry, _runner
from tests.gateway.test_reset_button_deadlock import _make_event, _make_runner_with_cached_agent

_PROBE = '''from hermes_constants import get_hermes_home
import json

def register(ctx):
    def ingress(**kwargs):
        (get_hermes_home() / 'ingress-marker').write_text('observed')
    def reset(session_key=None, new_session_id=None, command_metadata=None, **kwargs):
        (get_hermes_home() / 'reset-evidence.json').write_text(json.dumps({
            'session_key': session_key, 'new_session_id': new_session_id,
            'command_metadata': command_metadata,
        }))
        if command_metadata and command_metadata.get('prompt'):
            return ctx.inject_message(command_metadata['prompt'], session_key=session_key,
                                      expected_session_id=new_session_id)
    ctx.register_hook('pre_gateway_dispatch', ingress)
    ctx.register_hook('on_session_reset', reset)
'''


def _install_probe(home):
    home.mkdir(parents=True, exist_ok=True)
    plugin = home / 'plugins' / 'reset-probe'
    plugin.mkdir(parents=True)
    (plugin / 'plugin.yaml').write_text('name: reset-probe\nversion: 1.0.0\n')
    (plugin / '__init__.py').write_text(_PROBE)
    (home / 'config.yaml').write_text('plugins:\n  enabled: [reset-probe]\n  entries:\n    reset-probe:\n      allow_gateway_injection: true\n')
    with _profile_runtime_scope(home):
        manager = get_plugin_manager()
        manager.discover_and_load(force=True)
    assert not manager._plugins['reset-probe'].error
    return manager


@pytest.mark.asyncio
async def test_pre_dispatch_observes_only_routed_owner_and_restores_scope(tmp_path):
    launch, owner = tmp_path / 'launch', tmp_path / 'owner'
    _install_probe(launch)
    _install_probe(owner)
    runner = _make_runner_with_cached_agent(lambda: None)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._resolve_profile_home_for_source = lambda _: owner
    event = _make_event('/new (hello)')
    event.source.profile = 'owner'
    with _profile_runtime_scope(launch):
        assert await runner._hm_pre_gateway_dispatch_hook(event, event.source) is event
        assert get_hermes_home() == launch
    assert (owner / 'ingress-marker').exists()
    assert not (launch / 'ingress-marker').exists()


@pytest.mark.asyncio
async def test_reset_reports_its_command_and_new_session_to_owner(tmp_path):
    launch, owner = tmp_path / 'launch', tmp_path / 'owner'
    _install_probe(launch)
    _install_probe(owner)
    runner = _make_runner_with_cached_agent(lambda: None)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._resolve_profile_home_for_source = lambda _: owner
    runner._reset_notice_session_info = lambda _: ''
    event = _make_event('/new')
    event.source.profile = 'owner'
    event.metadata = {'correlation': 'this-confirmed-reset'}
    with _profile_runtime_scope(launch):
        await runner._handle_reset_command(event)
        assert get_hermes_home() == launch
    evidence = json.loads((owner / 'reset-evidence.json').read_text())
    assert evidence == {'session_key': runner._session_key_for_source(event.source),
                        'new_session_id': 'sess-new', 'command_metadata': event.metadata}
    assert not (launch / 'reset-evidence.json').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('rotate_before_dispatch', [False, True])
async def test_discovered_plugin_fences_dispatch_against_the_confirmed_session(tmp_path, monkeypatch, rotate_before_dispatch):
    home = tmp_path / 'owner'
    monkeypatch.setenv('HERMES_HOME', str(home))
    manager = _install_probe(home)
    entry = _entry()
    confirmed_id = entry.session_id
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(entry, adapter)
    runner._gateway_loop = asyncio.get_running_loop()
    runner.session_store.lookup_by_session_key = lambda _: entry
    manager.set_gateway_message_injector(runner, runner._schedule_plugin_message_injection)
    with _profile_runtime_scope(home):
        assert manager.invoke_hook('on_session_reset', session_key=entry.session_key,
                                   new_session_id=confirmed_id, command_metadata={'prompt': 'hello'}) == [True]
        if rotate_before_dispatch:
            entry.session_id = 'subsequent-reset'
        await asyncio.gather(*tuple(runner._background_tasks))
    if rotate_before_dispatch:
        adapter.handle_message.assert_not_awaited()
    else:
        event = adapter.handle_message.await_args.args[0]
        assert event.text == 'hello'
        assert event.metadata['gateway_session_id'] == confirmed_id
        assert event.metadata['gateway_session_strict'] is True
        assert event.internal and not event.allow_gateway_control


@pytest.mark.parametrize('session_key', [None, 'agent:main:telegram:dm:42'])
def test_fenced_injection_never_falls_back_to_cli_or_tui(tmp_path, session_key):
    manager = _install_probe(tmp_path / 'owner')
    cli = SimpleNamespace(_agent_running=False, _pending_input=SimpleQueue())
    manager._cli_ref = cli
    tui = MagicMock(return_value=True)
    manager.set_tui_message_injector(object(), tui)
    assert manager.invoke_hook('on_session_reset', session_key=session_key,
                               new_session_id='confirmed', command_metadata={'prompt': 'hello'}) == [False]
    assert cli._pending_input.empty()
    tui.assert_not_called()
