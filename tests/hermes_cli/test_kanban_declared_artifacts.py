"""Declared evidence survives native review handoff and scratch cleanup."""
from pathlib import Path

import pytest
import run_agent
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as workspace


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / '.hermes'
    root.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(root))
    for key in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_TASK', 'HERMES_DELEGATED_CHILD_CONTEXT'):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    return root


def test_missing_declared_evidence_prevents_acceptance_and_preserves_workspace(home):
    with kbc.connect_closing() as conn:
        card = kb.create_task(conn, title='Bounded result with one required artifact')
        claimed = kb.claim_task(conn, card)
        scratch = workspace.resolve_workspace(claimed)
        workspace.set_workspace_path(conn, card, scratch)
        required = scratch / 'required.txt'
        with pytest.raises(kb.ArtifactPreservationError, match='unavailable'):
            kb.complete_task(conn, card, expected_run_id=claimed.current_run_id,
                             summary='Required result claimed.', metadata={'artifacts': [str(required)]})
        assert kb.get_task(conn, card).status == 'running'
        assert scratch.is_dir()
        required.write_text('actual required result')
        assert kb.complete_task(conn, card, expected_run_id=claimed.current_run_id,
                                summary='Required result verified.', metadata={'artifacts': [str(required)]})
        assert not scratch.exists()
        preserved = next(item for item in kb.list_attachments(conn, card) if item.filename == 'required.txt')
        assert Path(preserved.stored_path).read_text() == 'actual required result'


@pytest.mark.parametrize('lose_durable', [False, True])
def test_external_file_and_existing_handoff_survive_review_without_duplicate_copy(home, lose_durable):
    outside = home.parent / 'external-result.txt'
    outside.write_text('actual external result')
    reference = 'https://example.invalid/related-evidence'
    with kbc.connect_closing() as conn:
        card = kb.create_task(conn, title='Review an external file and a later scratch result')
        claimed = kb.claim_task(conn, card)
        scratch = workspace.resolve_workspace(claimed)
        workspace.set_workspace_path(conn, card, scratch)
        assert kb.request_review(conn, card, reviewer='default', summary='External result ready.',
                                 metadata={'artifacts': [str(outside), reference]},
                                 expected_run_id=claimed.current_run_id)
        metadata = dict(kb.latest_run(conn, card).metadata)
        second = scratch / 'review-result.txt'
        second.write_text('actual review result')
        metadata['artifacts'] = [str(second), *metadata['artifacts']]
        review = kb.claim_review_task(conn, card)
        if lose_durable:
            original = Path(kb.list_attachments(conn, card)[0].stored_path)
            original.unlink()
            with pytest.raises(kb.ArtifactPreservationError, match='unavailable'):
                kb.complete_task(conn, card, summary='Required evidence was lost.', metadata=metadata,
                                 expected_run_id=review.current_run_id)
            assert kb.get_task(conn, card).status == 'running'
            assert scratch.is_dir()
            assert len(kb.list_attachments(conn, card)) == 1
            assert not (original.parent / second.name).exists()
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_text('actual external result')
        assert kb.complete_task(conn, card, summary='Both results verified.', metadata=metadata,
                                expected_run_id=review.current_run_id)
        attachments = kb.list_attachments(conn, card)
        assert {item.filename: Path(item.stored_path).read_text() for item in attachments} == {
            'external-result.txt': 'actual external result', 'review-result.txt': 'actual review result'}
        assert len(attachments) == 2
        assert reference in kb.latest_run(conn, card).metadata['artifacts']
        assert not scratch.exists()
        assert outside.read_text() == 'actual external result'
