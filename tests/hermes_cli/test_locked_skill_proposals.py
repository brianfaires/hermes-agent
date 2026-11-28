"""Locked proposals never confer a grant on the live package."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.platforms('posix')


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.STDOUT).decode().strip()


@pytest.fixture
def owned(tmp_path, monkeypatch):
    home = tmp_path / 'owner'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    git(home, 'init', '-b', 'main')
    git(home, 'config', 'user.email', 'fixture@example.invalid')
    git(home, 'config', 'user.name', 'Fixture')
    skill = home / 'skills/ops/example/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: example\ndescription: Example\n---\nOriginal\n')
    git(home, 'add', 'skills/ops/example/SKILL.md')
    git(home, 'commit', '-m', 'fixture')
    (home / 'skills/.usage.json').write_text(json.dumps({'example': {'locked': True}}))
    return home, skill, tmp_path / 'proposal'


def snapshot(home, skill):
    return (skill.read_bytes(), (home / 'skills/.usage.json').read_bytes(),
            git(home, 'rev-parse', 'HEAD'), (home / '.git/index').read_bytes())


def test_prepare_is_an_explicit_isolated_committed_source_operation(owned):
    from hermes_cli import locked_skill_proposals as proposals
    home, skill, destination = owned
    before = snapshot(home, skill)
    prepared = proposals.prepare(home=home, skill='ops/example', destination=destination)
    assert (destination / 'skills/ops/example/SKILL.md').read_bytes() == skill.read_bytes()
    assert prepared['base'] == git(home, 'rev-parse', 'HEAD')
    assert prepared['home'] == str(home)
    assert snapshot(home, skill) == before
    (destination / 'skills/ops/example/SKILL.md').write_text('Proposed only\n')
    assert snapshot(home, skill) == before


def test_freeze_binds_the_complete_single_file_revision(owned):
    from hermes_cli import locked_skill_proposals as proposals
    home, skill, destination = owned
    prepared = proposals.prepare(home=home, skill='ops/example', destination=destination)
    proposed = destination / 'skills/ops/example/SKILL.md'
    proposed.write_text(skill.read_text().replace('Original', 'Reviewed'))
    git(destination, 'add', 'skills/ops/example/SKILL.md')
    git(destination, 'commit', '-m', 'propose')
    before = snapshot(home, skill)
    frozen = proposals.freeze(prepared)
    assert frozen['head'] == git(destination, 'rev-parse', 'HEAD')
    assert frozen['post_sha256'] == hashlib.sha256(proposed.read_bytes()).hexdigest()
    assert frozen['manifest_sha256'] == proposals.manifest_digest(frozen)
    assert snapshot(home, skill) == before
    (destination / 'other').write_text('unrelated\n')
    git(destination, 'add', 'other')
    git(destination, 'commit', '-m', 'unrelated')
    with pytest.raises(ValueError):
        proposals.freeze(prepared)


@pytest.mark.parametrize('mutation', ['unlocked', 'corrupt', 'symlink', 'dirty'])
def test_prepare_refuses_unqualified_target_without_live_writes(owned, mutation):
    from hermes_cli import locked_skill_proposals as proposals
    home, skill, destination = owned
    usage = home / 'skills/.usage.json'
    if mutation == 'unlocked':
        usage.write_text('{"example":{"locked":false}}')
    elif mutation == 'corrupt':
        usage.write_text('broken')
    elif mutation == 'symlink':
        sibling = skill.with_name('other')
        sibling.write_bytes(skill.read_bytes())
        skill.unlink()
        skill.symlink_to(sibling)
    else:
        skill.write_text('uncommitted live bytes\n')
    before = snapshot(home, skill)
    with pytest.raises(ValueError):
        proposals.prepare(home=home, skill='ops/example', destination=destination)
    assert snapshot(home, skill) == before
    assert not destination.exists()


def test_duplicate_lock_records_fail_closed(owned):
    from hermes_cli import locked_skill_proposals as proposals
    home, skill, destination = owned
    (home / 'skills/.usage.json').write_text('{"example":{"locked":false},"example":{"locked":true}}')
    before = snapshot(home, skill)
    with pytest.raises(ValueError):
        proposals.prepare(home=home, skill='ops/example', destination=destination)
    assert snapshot(home, skill) == before


def test_public_cli_preparation_is_separate_from_native_approval():
    import argparse
    from hermes_cli.subcommands.skills import build_skills_parser
    parser = argparse.ArgumentParser()
    build_skills_parser(parser.add_subparsers(), cmd_skills=lambda args: None)
    args = parser.parse_args(['skills', 'proposal', 'prepare', 'ops/example', '/new/worktree'])
    assert args.proposal_action == 'prepare'
    assert args.skill == 'ops/example'
    with pytest.raises(SystemExit):
        parser.parse_args(['skills', 'proposal', 'approve', '0' * 64])
    with pytest.raises(SystemExit):
        parser.parse_args(['skills', 'proposal', 'enroll-finish', 'id', '0' * 64])


@pytest.mark.parametrize('mutation', ['script', 'new-file', 'delete', 'rename', 'symlink', 'mode', 'untracked'])
def test_freeze_rejects_every_non_single_regular_file_change(owned, mutation):
    from hermes_cli import locked_skill_proposals as proposals
    home, skill, destination = owned
    prepared = proposals.prepare(home=home, skill='ops/example', destination=destination)
    target = destination / 'skills/ops/example/SKILL.md'
    target.write_text('approved candidate\n')
    def symlink():
        target.unlink()
        target.symlink_to('other.md')
    changes = {'delete': target.unlink, 'rename': lambda: target.rename(target.with_name('renamed.md')),
               'symlink': symlink, 'mode': lambda: target.chmod(0o755)}
    if mutation in changes:
        changes[mutation]()
    else:
        extra = destination / ('skills/ops/example/scripts/new.py' if mutation == 'script' else 'other')
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text('unrelated\n')
    git(destination, 'add', 'skills/ops/example/SKILL.md' if mutation == 'untracked' else '.')
    git(destination, 'commit', '-m', 'not a qualified revision')
    before = snapshot(home, skill)
    with pytest.raises(ValueError):
        proposals.freeze(prepared)
    assert snapshot(home, skill) == before


@pytest.mark.parametrize('field', ['home', 'repository', 'owner', 'profile_id', 'skill', 'path', 'base', 'common_dir'])
def test_prepared_identity_cannot_be_retargeted(owned, field):
    from hermes_cli import locked_skill_proposals as proposals
    home, skill, destination = owned
    prepared = proposals.prepare(home=home, skill='ops/example', destination=destination)
    (destination / 'skills/ops/example/SKILL.md').write_text('candidate\n')
    git(destination, 'add', '.')
    git(destination, 'commit', '-m', 'candidate')
    prepared[field] = 'different-target'
    before = snapshot(home, skill)
    with pytest.raises((ValueError, OSError)):
        proposals.freeze(prepared)
    assert snapshot(home, skill) == before


def test_configured_submodule_ignore_cannot_hide_a_changed_gitlink(owned):
    from hermes_cli import locked_skill_proposals as proposals
    home, skill, destination = owned
    initial = git(home, 'rev-parse', 'HEAD')
    git(home, 'update-index', '--add', '--cacheinfo', '160000', initial, 'vendor/module')
    git(home, 'commit', '-m', 'tracked existing gitlink')
    prepared = proposals.prepare(home=home, skill='ops/example', destination=destination)
    (destination / 'skills/ops/example/SKILL.md').write_text('candidate\n')
    git(destination, 'add', 'skills/ops/example/SKILL.md')
    git(destination, 'update-index', '--cacheinfo', '160000', prepared['base'], 'vendor/module')
    git(destination, 'commit', '-m', 'skill plus prohibited gitlink change')
    git(destination, 'config', 'diff.ignoreSubmodules', 'all')
    before = snapshot(home, skill)
    with pytest.raises(ValueError):
        proposals.freeze(prepared)
    assert snapshot(home, skill) == before
