"""The native process-list viewer masks complete context before its tail cut."""

from agent import redact
from tools import process_registry as pr


def test_process_list_masks_tail_and_preview_before_selection(monkeypatch):
    from tui_gateway import server

    monkeypatch.setattr(redact, '_REDACT_ENABLED', True)
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', registry)
    registry._running['proc_owned'] = pr.ProcessSession(
        id='proc_owned', command='printenv', session_key='owner',
        output_buffer='API_KEY="' + 'opaque-dock-secret-' * 400 + '"\nuseful completion\n')
    registry._running['proc_foreign'] = pr.ProcessSession(
        id='proc_foreign', command='worker', session_key='other', output_buffer='OTHER-SESSION')
    rows = server._session_processes({'session_key': 'owner'})
    assert [row['session_id'] for row in rows] == ['proc_owned']
    assert 'opaque-dock-secret' not in rows[0]['output_tail'] + rows[0]['output_preview']
    assert 'useful completion' in rows[0]['output_tail']
