"""Text-mode slash commands preserve multiline notes through existing dispatch."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cli import HermesCLI
from gateway.run import _profile_runtime_scope
from hermes_cli.plugins import get_plugin_manager


def install(home):
    plugin = home / 'plugins' / 'text-example'
    plugin.mkdir(parents=True)
    (plugin / 'plugin.yaml').write_text('name: text-example\nversion: 1.0.0\n')
    (plugin / '__init__.py').write_text(
        'from hermes_constants import get_hermes_home\n'
        'def capture(raw):\n'
        '    (get_hermes_home() / "captured.txt").write_text(raw)\n'
        '    return "Logged"\n'
        'def register(ctx):\n'
        '    ctx.register_command("log", capture, argument_mode="text")\n'
        '    ctx.register_command("options-example", capture, argument_mode="options")\n')
    (home / 'config.yaml').write_text('plugins:\n  enabled: [text-example]\n')
    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    assert not manager._plugins['text-example'].error


def test_cli_text_command_preserves_note_and_legacy_options_stay_trimmed(tmp_path):
    for label in ('first', 'second'):
        home = tmp_path / label
        with _profile_runtime_scope(home):
            install(home)
            cli = object.__new__(HermesCLI)
            cli.config = {}
            cli._slash_metrics_surface = None
            cli._pending_resume_sessions = None
            cli.agent = None
            cli.chat = Mock(side_effect=AssertionError('no model turn'))
            assert cli.process_command('/log  café\n  indented\n\nend \n')
            assert (home / 'captured.txt').read_text() == ' café\n  indented\n\nend \n'
            assert cli.process_command('/options-example  option \n')
            assert (home / 'captured.txt').read_text() == 'option'
            cli.chat.assert_not_called()


def test_cli_enter_and_editor_preserve_trailing_newlines(tmp_path):
    from prompt_toolkit.buffer import Buffer
    import queue

    cli = object.__new__(HermesCLI)
    cli._tui_enter_overlay = lambda event: False
    cli._tui_multiline_shortcuts = False
    cli._tui_enter_inline_command = lambda *args: False
    cli._attached_images = []
    cli._agent_running = True
    cli._pending_input = queue.Queue()
    cli._inline_pastes = lambda buffer: None
    buffer = Buffer()
    note = '/log  pasted\n  line\n'
    buffer.text = note
    event = SimpleNamespace(app=SimpleNamespace(current_buffer=buffer, invalidate=Mock()))
    cli._tui_handle_enter(event)
    assert cli._pending_input.get_nowait() == note
    buffer.text = note
    cli._expand_paste_references = lambda text: text
    cli.handle_bang_shell = lambda text: False
    cli.process_command = Mock(return_value=True)
    cli._reset_input_buffer = Mock()
    cli._submit_editor_buffer(buffer)
    cli.process_command.assert_called_once_with(note)


@pytest.mark.asyncio
async def test_owner_slash_dispatch_preserves_text_and_refuses_another_user(tmp_path):
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import LocalSessionAdapter
    from hermes_cli.plugin_command_dispatch import invoke_plugin_command
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch

    with _profile_runtime_scope(tmp_path):
        install(tmp_path)
        with SessionDB(db_path=tmp_path / 'state.db') as db:
            db.create_session('s', source='test')
            epoch = begin_runtime_epoch(db, instance_id='current')
            runner = SimpleNamespace(_draining=False, session_store=SimpleNamespace(lookup_by_session_key=lambda key: None))
            async def run_handler(handler, event, source, entry, *, args):
                return invoke_plugin_command(handler, args, source=source)
            runner._hm_run_plugin_handler = run_handler
            authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='current', db=db, epoch=epoch)
            runner.session_authority = authority
            adapter = LocalSessionAdapter(authority)
            runner._adapters_for_profile = lambda profile: {Platform.LOCAL: adapter}
            owner = AuthorityConnection(authority, object(), {'user_id': 'owner'})
            source = SessionSource(platform=Platform.LOCAL, chat_id='local-s', user_id=owner.actor.subject)
            adapter.register_source(source)
            adapter.policies[source.chat_id] = SimpleNamespace(cwd=str(tmp_path), terminal_json="{}")
            runner._adapter_for_source = lambda source: adapter
            authority.sessions['s'] = LiveSession(source, 'route')
            stranger = AuthorityConnection(authority, object(), {'user_id': 'other'})
            params = {'session_id': 's', 'command': 'log  note\n  line\n'}
            denied = await stranger.dispatch({'id': 1, 'method': 'slash.exec', 'params': params})
            assert 'error' in denied
            assert not (tmp_path / 'captured.txt').exists()
            reply = await owner.dispatch({'id': 2, 'method': 'slash.exec', 'params': params})
            assert reply.get('result', {}).get('output') == 'Logged', reply
            assert (tmp_path / 'captured.txt').read_text() == ' note\n  line\n'


@pytest.mark.parametrize('prefix', ['', '/log '])
def test_classic_paste_keeps_full_multiline_command(prefix):
    from prompt_toolkit.buffer import Buffer

    cli = object.__new__(HermesCLI)
    cli.config = {'paste_collapse_threshold': 1}
    cli._tui_collapse_paste = Mock(side_effect=AssertionError('command must stay text'))
    buffer = Buffer()
    buffer.text = prefix
    buffer.cursor_position = len(prefix)
    pasted = ('/log ' if not prefix else '') + '  café\n\n  pasted\n'
    event = SimpleNamespace(data=pasted, current_buffer=buffer, app=SimpleNamespace(invalidate=Mock()))
    cli._tui_handle_paste(event)
    assert buffer.text == prefix + pasted
