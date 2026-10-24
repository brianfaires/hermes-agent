"""Interactive skill projections share current runtime relevance gates."""
import pytest

from agent import skill_commands
from agent.delegation_context import non_dispatcher_owned_context
from hermes_cli import plugins
from tests.agent.test_skill_environment_warm_cache import _skills


@pytest.mark.parametrize('surface', ['local', 'plugin'])
def test_interactive_projection_drops_worker_only_skill_on_context_change(tmp_path, monkeypatch, surface):
    _skills(tmp_path, monkeypatch)
    package = tmp_path / 'plugins' / 'environment-probe'
    package.mkdir(parents=True)
    (package / 'plugin.yaml').write_text('name: environment-probe\nversion: 0.1.0\n', encoding='utf-8')
    (package / '__init__.py').write_text(
        'from pathlib import Path\ndef register(ctx):\n'
        '    ctx.register_skill("worker-guide", Path(__file__).parent / "guide.md")\n', encoding='utf-8')
    (package / 'guide.md').write_text(
        '---\nname: worker-guide\ndescription: Worker guidance.\nenvironments: [kanban]\n---\nRead the task.\n',
        encoding='utf-8')
    with (tmp_path / 'config.yaml').open('a', encoding='utf-8') as stream:
        stream.write('plugins:\n  enabled: [environment-probe]\n')
    monkeypatch.setattr(skill_commands, '_skill_commands_by_key', {})
    plugins._reset_plugin_managers_for_tests()
    skill_commands.invalidate_plugin_skill_commands()
    getter, command = ((skill_commands.get_skill_commands, '/worker-only-guide') if surface == 'local'
                       else (skill_commands.get_plugin_skill_commands, '/environment-probe:worker-guide'))
    try:
        monkeypatch.setenv('HERMES_KANBAN_TASK', 'synthetic-card')
        assert command in getter()
        with non_dispatcher_owned_context():
            assert command not in getter()
        assert command in getter()
        monkeypatch.delenv('HERMES_KANBAN_TASK')
        assert command not in getter()
    finally:
        plugins._reset_plugin_managers_for_tests()
        skill_commands.invalidate_plugin_skill_commands()


def test_change_watcher_tracks_installs_after_a_worker_command_scan(tmp_path, monkeypatch):
    _skills(tmp_path, monkeypatch)
    monkeypatch.setattr(skill_commands, '_skill_commands_by_key', {})
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'synthetic-card')
    assert '/worker-only-guide' in skill_commands.get_skill_commands()
    before = skill_commands.published_scopes_signature()
    skill = tmp_path / 'skills' / 'new-guide' / 'SKILL.md'
    skill.parent.mkdir()
    skill.write_text('---\nname: new-guide\ndescription: New guidance.\n---\nRead the task.\n')
    assert skill_commands.published_scopes_signature() != before
    assert '/new-guide' in skill_commands.get_skill_commands()
