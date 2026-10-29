"""Task-owner admission resolves optional pins in the selected profile and workspace."""
import hermes_bootstrap
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.session_contract import Principal
from gateway.session_kanban import build_kanban_policy, _run_task_turns
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing


def test_native_owner_skips_retired_optional_pin_without_changing_card(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda name: str(tmp_path))
    kb.create_board('owned')
    workspace = tmp_path / 'work'
    workspace.mkdir()
    with connect_closing(board='owned') as conn:
        task_id = kb.create_task(conn, title='Execute ordinary work', assignee='default',
            workspace_kind='dir', workspace_path=str(workspace), skills=['retired-optional-fixture'])
        task = kb.claim_task(conn, task_id)
        actor = Principal('owner', str(tmp_path), frozenset({'session:create'}), 'transport')
        connection = SimpleNamespace(authority=SimpleNamespace(profile_id=str(tmp_path)), actor=actor, native_owner=True)
        params = dict(board='owned', task_id=task.id, run_id=task.current_run_id, claim_lock=task.claim_lock)
        policy, _ = build_kanban_policy(connection, params, {'model': {'default': 'fixture', 'provider': 'custom'}})
        context = json.loads(policy.kanban_json)
        agent = SimpleNamespace(session_id='fixture-worker', run_conversation=Mock(
            return_value={'failed': False, 'final_response': 'Finished fixture work.'}))
        result = _run_task_turns(agent, {'text': 'Perform fixture work'}, [], context)
        assert result['failed'] is False
        agent.run_conversation.assert_called_once()
        assert kb.get_task(conn, task_id).skills == ['retired-optional-fixture']


def test_skill_availability_checks_do_not_execute_inline_commands(tmp_path, monkeypatch):
    import shlex
    from hermes_cli.kanban_launch import resolve_worker_skills
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda name: str(tmp_path))
    (tmp_path / 'config.yaml').write_text('skills:\n  inline_shell: true\n')
    counter = tmp_path / 'activations'
    skill = tmp_path / 'skills' / 'inline-fixture'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: inline-fixture\ndescription: Counter fixture\ndeps: [fixture-tool]\n---\n'
        + '!`printf x >> ' + shlex.quote(str(counter)) + '`\n')
    ensure = Mock()
    monkeypatch.setattr('pm.ensure', ensure)
    kb.create_board('owned')
    workspace = tmp_path / 'work'
    workspace.mkdir()
    with connect_closing(board='owned') as conn:
        task_id = kb.create_task(conn, title='Run skill once', assignee='default',
            workspace_kind='dir', workspace_path=str(workspace), skills=['inline-fixture'])
        task = kb.claim_task(conn, task_id)
        assert resolve_worker_skills(task, str(tmp_path), [], workspace=str(workspace)) == ['inline-fixture']
        assert not counter.exists(), 'Checking skill availability executed its command'
        actor = Principal('owner', str(tmp_path), frozenset({'session:create'}), 'transport')
        connection = SimpleNamespace(authority=SimpleNamespace(profile_id=str(tmp_path)), actor=actor, native_owner=True)
        params = dict(board='owned', task_id=task.id, run_id=task.current_run_id, claim_lock=task.claim_lock)
        policy, _ = build_kanban_policy(connection, params, {'model': {'default': 'fixture', 'provider': 'custom'}})
        assert not counter.exists(), 'Owner admission executed the skill before its worker'
        ensure.assert_not_called()
        agent = SimpleNamespace(session_id='fixture-worker', run_conversation=Mock(
            return_value={'failed': False, 'final_response': 'Finished.'}))
        _run_task_turns(agent, {'text': 'Use fixture skill'}, [], json.loads(policy.kanban_json))
        assert counter.read_text() == 'x'
        ensure.assert_called_once_with('fixture-tool')


@pytest.mark.parametrize('change', ['optional-disabled', 'required-disabled', 'required-ambiguous'])
def test_worker_rechecks_required_vs_optional_after_admission(tmp_path, monkeypatch, change):
    review = change.startswith('required')
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_BOARD', raising=False)
    optional = 'optional-fixture'
    for name in (optional, 'sdlc-review'):
        skill = tmp_path / 'skills' / name
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Fixture skill\n---\nFixture instructions.\n')
    kb.create_board('owned')
    with connect_closing(board='owned') as conn:
        workspace = tmp_path / 'work'
        workspace.mkdir()
        task_id = kb.create_task(conn, title='Respect role requirements', assignee='default', skills=[optional],
                                 workspace_kind='dir', workspace_path=str(workspace))
        if review:
            assert kb.request_review(conn, task_id, reviewer='default', summary='Ready for review.')
        task = kb.claim_review_task(conn, task_id) if review else kb.claim_task(conn, task_id)
        actor = Principal('owner', str(tmp_path), frozenset({'session:create'}), 'transport')
        connection = SimpleNamespace(authority=SimpleNamespace(profile_id=str(tmp_path)), actor=actor, native_owner=True)
        params = dict(board='owned', task_id=task.id, run_id=task.current_run_id, claim_lock=task.claim_lock)
        policy, _ = build_kanban_policy(connection, params, {'model': {'default': 'fixture', 'provider': 'custom'}})
        disabled = 'sdlc-review' if review else optional
        if change == 'required-ambiguous':
            import shutil
            shutil.rmtree(tmp_path / 'skills' / 'sdlc-review')
            for category in ('first', 'second'):
                skill = tmp_path / 'skills' / category / 'sdlc-review'
                skill.mkdir(parents=True)
                (skill / 'SKILL.md').write_text('---\nname: sdlc-review\ndescription: Fixture\n---\nReview guidance.')
        else:
            (tmp_path / 'config.yaml').write_text(f'skills:\n  disabled: [{disabled}]\n')
        agent = SimpleNamespace(session_id='fixture-worker', run_conversation=Mock(
            return_value={'failed': False, 'final_response': 'Finished.'}))
        if review:
            with pytest.raises(ValueError, match='kanban_skills_unavailable'):
                _run_task_turns(agent, {'text': 'Do the work'}, [], json.loads(policy.kanban_json))
            agent.run_conversation.assert_not_called()
        else:
            result = _run_task_turns(agent, {'text': 'Do the work'}, [], json.loads(policy.kanban_json))
            assert result['failed'] is False
            agent.run_conversation.assert_called_once()


def test_owner_run_follows_the_owning_profile_lane_skills(tmp_path, monkeypatch):
    """The owner-run path applies the same kanban.lane_skills as the dispatcher, not a fixed skill."""
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_BOARD', raising=False)
    (tmp_path / 'config.yaml').write_text('kanban:\n  lane_skills: {review: [], ready: [org-rules]}\n')
    kb.create_board('owned')
    review_ws, build_ws = tmp_path / 'review', tmp_path / 'build'
    review_ws.mkdir()
    build_ws.mkdir()
    actor = Principal('owner', str(tmp_path), frozenset({'session:create'}), 'transport')
    connection = SimpleNamespace(authority=SimpleNamespace(profile_id=str(tmp_path)), actor=actor, native_owner=True)
    with connect_closing(board='owned') as conn:
        def policy_for(task):
            params = dict(board='owned', task_id=task.id, run_id=task.current_run_id, claim_lock=task.claim_lock)
            policy, _ = build_kanban_policy(connection, params, {'model': {'default': 'fixture', 'provider': 'custom'}})
            return json.loads(policy.kanban_json)
        reviewed = kb.create_task(conn, title='Review it', assignee='default',
                                  workspace_kind='dir', workspace_path=str(review_ws))
        assert kb.request_review(conn, reviewed, reviewer='default', summary='Ready for review.')
        assert policy_for(kb.claim_review_task(conn, reviewed))['required_skills'] == []
        built = kb.create_task(conn, title='Build it', assignee='default',
                               workspace_kind='dir', workspace_path=str(build_ws))
        with pytest.raises(Exception, match='kanban_skills_unavailable'):
            policy_for(kb.claim_task(conn, built))
