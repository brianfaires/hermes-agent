"""Legacy TUI slash RPC retains text without a second command protocol."""
from gateway.run import _profile_runtime_scope
from tests.hermes_cli.test_plugin_text_commands import install
from tui_gateway import server


def test_slash_exec_runs_text_plugin_in_the_selected_profile(tmp_path):
    launch, selected = tmp_path / 'launch', tmp_path / 'selected'
    launch.mkdir()
    with _profile_runtime_scope(selected):
        install(selected)
    with _profile_runtime_scope(launch):
        server._sessions['text-test'] = {'session_key': 'text-test', 'profile_home': str(selected), 'agent': None}
        try:
            reply = server.handle_request({'id': 1, 'method': 'slash.exec', 'params': {
                'session_id': 'text-test', 'command': 'log  note\n  line\n'}})
            assert reply.get('result', {}).get('output') == 'Logged', reply
            assert not (launch / 'captured.txt').exists()
            assert (selected / 'captured.txt').read_text() == ' note\n  line\n'
        finally:
            server._sessions.pop('text-test', None)
