"""Recovery preserves authorized work, newer instructions and durable-effect checks."""
import pytest

from gateway.run import _prepare_resume_pending_message


@pytest.mark.parametrize('interactive', [False, True])
def test_auto_resume_preserves_authorization_and_reconciles_uncertain_effects(interactive):
    note, persisted = _prepare_resume_pending_message('restart_timeout', '', interactive=interactive)
    assert 'CONTINUE the interrupted task' in note
    assert 'does not itself revoke approval' in note
    assert 'Respect current holds' in note
    assert 'inspect durable receipts' in note
    assert 'do not replay those mutations' in note
    assert persisted == note


def test_newer_stop_takes_priority_without_persisting_recovery_guidance_as_user_text():
    message = 'Stop the earlier task.'
    note, persisted = _prepare_resume_pending_message('shutdown_timeout', message)
    assert 'NEW message below FIRST' in note
    assert 'stop, cancellation' in note
    assert 'takes priority' in note
    assert 'inspect durable receipts' in note
    assert note.endswith(message)
    assert persisted == message
