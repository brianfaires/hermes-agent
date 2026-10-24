"""A warm native scan resolves relative skill roots under each served profile."""
import os

from gateway.run import _profile_runtime_scope
from agent.skill_utils import get_external_skills_dirs, _external_dirs_cache_clear
from tools.skills_tool import find_all_skills


def test_relative_roots_and_skill_discovery_stay_profile_local_with_warm_cache(tmp_path, monkeypatch):
    launch = tmp_path / 'launch'
    launch.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(launch))
    homes = {}
    for label in ('a', 'b'):
        home = tmp_path / label
        (home / 'skills').mkdir(parents=True)
        (home / 'config.yaml').write_text('skills:\n  external_dirs: [external-skills]\n')
        skill = home / 'external-skills' / ('owner-' + label)
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: owner-' + label
            + '\ndescription: Profile-local fixture skill.\n---\n\nUse only this owning fixture.\n')
        homes[label] = home
    _external_dirs_cache_clear()
    for label in ('a', 'b', 'a'):
        home = homes[label]
        with _profile_runtime_scope(home):
            expected = [home / 'external-skills']
            assert get_external_skills_dirs() == expected
            assert get_external_skills_dirs() == expected
            names = {entry['name'] for entry in find_all_skills()}
            assert 'owner-' + label in names
            assert 'owner-' + ('b' if label == 'a' else 'a') not in names
            assert os.environ['HERMES_HOME'] == str(launch)
