"""Managed owner-map qualification uses disposable roots and repositories only."""
import json

import pytest

from tests.hermes_cli.test_locked_skill_proposals import snapshot

pytest_plugins = ['tests.hermes_cli.test_locked_skill_proposals']
pytestmark = pytest.mark.platforms('posix')


@pytest.fixture
def managed(owned):
    home = owned[0]
    root = home.parent
    foreign = root / 'foreign-owner'
    foreign.mkdir()
    directory = root / '.config-repo'
    directory.mkdir()
    path = directory / 'workspace.json'
    manifest = {'root': '..', 'repositories': {
        'fixture': {'worktree': home.name, 'git_dir': home.name + '/.git', 'repository': 'fixture/owner',
                    'owner': 'fixture-owner', 'paths': ['skills']},
        'foreign': {'worktree': foreign.name, 'git_dir': 'foreign.git', 'repository': 'fixture/foreign',
                    'owner': 'other-owner', 'paths': ['config.yaml']}}}
    path.write_text(json.dumps(manifest))
    return owned, path, manifest, foreign


def test_managed_owner_binding_and_all_live_destination_roots(managed):
    from hermes_cli.locked_skill_proposals import prepare
    (home, skill, destination), _path, _manifest, foreign = managed
    before = snapshot(home, skill)
    with pytest.raises(ValueError):
        prepare(home=home, skill='ops/example', destination=foreign / 'proposal')
    assert not (foreign / 'proposal').exists()
    prepared = prepare(home=home, skill='ops/example', destination=destination)
    assert prepared['repository'] == 'fixture/owner'
    assert prepared['owner'] == 'fixture-owner'
    assert prepared['profile_id'] == prepared['home'] == str(home)
    assert prepared['git_dir'] == str(home / '.git')
    assert snapshot(home, skill) == before


@pytest.mark.parametrize('mutation', ['ambiguous', 'wrong-owner'])
def test_managed_owner_map_refuses_ambiguous_or_wrong_home(managed, mutation):
    from hermes_cli.locked_skill_proposals import prepare
    (home, skill, destination), path, manifest, foreign = managed
    if mutation == 'ambiguous':
        manifest['repositories']['duplicate'] = manifest['repositories']['fixture'].copy()
    else:
        manifest['repositories']['fixture']['worktree'] = foreign.name
    path.write_text(json.dumps(manifest))
    before = snapshot(home, skill)
    with pytest.raises(ValueError):
        prepare(home=home, skill='ops/example', destination=destination)
    assert not destination.exists()
    assert snapshot(home, skill) == before
