"""Runtime relevance gates survive warm prompt snapshots and catalog caches."""
import json

import pytest

from agent import prompt_builder as pb
from agent.delegation_context import non_dispatcher_owned_context
from tools import skills_tool


def _skills(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_PLATFORM', 'cli')
    for name in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_BOARD', 'HERMES_DELEGATED_CHILD_CONTEXT'):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / 'config.yaml').write_text('toolsets: [skills]\nplatform_toolsets:\n  cli: [skills]\n', encoding='utf-8')
    root = tmp_path / 'skills'
    for name, gate in (('worker-only-guide', 'environments: [kanban]\n'), ('ordinary-guide', '')):
        path = root / name / 'SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text('---\nname: ' + name + '\ndescription: Useful instructions for ' + name + '.\n'
                        + gate + '---\n# Guide\nRead the task before acting.\n', encoding='utf-8')
    pb.clear_skills_system_prompt_cache(clear_snapshot=True)
    skills_tool.clear_skills_cache()
    return root


@pytest.mark.parametrize('worker_first', [True, False])
def test_prompt_memory_and_disk_reuse_respect_current_environment(tmp_path, monkeypatch, worker_first):
    root = _skills(tmp_path, monkeypatch)
    def build(worker):
        if worker:
            monkeypatch.setenv('HERMES_KANBAN_TASK', 'synthetic-card')
        else:
            monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
        result = pb._build_skills_system_prompt_inner(root, [], None, None, None)
        assert 'ordinary-guide' in result
        assert ('worker-only-guide' in result) is worker
        if worker:
            assert 'Useful instructions for worker-only-guide.' in result
        return result
    try:
        initial = build(worker_first)
        build(not worker_first)
        assert build(worker_first) == initial
        pb.clear_skills_system_prompt_cache()
        build(not worker_first)
        build(worker_first)
        # A retained v4 artifact lacks the new gate metadata and must rebuild.
        path = pb._skills_prompt_snapshot_path()
        legacy = json.loads(path.read_text(encoding='utf-8-sig'))
        legacy['version'] = 4
        for entry in legacy['skills']:
            entry.pop('environments', None)
        path.write_text(json.dumps(legacy), encoding='utf-8')
        pb.clear_skills_system_prompt_cache()
        build(False)
        build(True)
    finally:
        pb.clear_skills_system_prompt_cache(clear_snapshot=True)
        skills_tool.clear_skills_cache()


def test_catalog_rechecks_worker_ownership_but_explicit_load_remains_available(tmp_path, monkeypatch):
    _skills(tmp_path, monkeypatch)
    monkeypatch.setenv('HERMES_KANBAN_TASK', 'synthetic-card')
    try:
        def names():
            return {skill['name'] for skill in skills_tool._find_all_skills()}
        assert 'worker-only-guide' in names()
        with non_dispatcher_owned_context():
            assert 'worker-only-guide' not in names()
            result = json.loads(skills_tool.skill_view('worker-only-guide'))
            assert result['success']
            assert 'Read the task before acting.' in result['content']
        assert 'worker-only-guide' in names()
        monkeypatch.delenv('HERMES_KANBAN_TASK')
        assert 'worker-only-guide' not in names()
    finally:
        pb.clear_skills_system_prompt_cache(clear_snapshot=True)
        skills_tool.clear_skills_cache()
