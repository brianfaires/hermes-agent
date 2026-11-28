"""Proposal subprocesses bind owner credentials, never the gateway's launch profile."""
from types import SimpleNamespace

import pytest


@pytest.fixture
def scoped_homes(tmp_path, monkeypatch):
    from tools.environments import local
    from hermes_cli import locked_skill_proposals, locked_skill_publication
    import hermes_constants
    primary, owner = tmp_path / 'primary', tmp_path / 'owner'
    primary.mkdir()
    owner.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(owner))
    monkeypatch.setattr(hermes_constants, 'get_routing_process_hermes_home', lambda: primary)
    monkeypatch.setenv('GH_TOKEN', 'fixture-launch-credential')
    monkeypatch.setenv('GH_CONFIG_DIR', str(primary / 'private-gh-config'))
    captured = []
    def run(command, **kwargs):
        captured.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'{}')
    monkeypatch.setattr(locked_skill_publication.subprocess, 'run', run)
    return owner, captured


def test_local_git_subprocess_never_inherits_launch_credentials(scoped_homes):
    from hermes_cli.locked_skill_proposals import git
    owner, captured = scoped_homes
    git({'home': str(owner)}, 'rev-parse', 'HEAD')
    env = captured[-1][1]['env']
    assert env['HERMES_HOME'] == str(owner)
    assert 'GH_TOKEN' not in env


def test_routed_pr_read_requires_own_credentials_not_launch_oauth(scoped_homes):
    from hermes_cli.locked_skill_publication import _read_pull
    from hermes_cli.locked_skill_proposals import ProposalError
    _owner, captured = scoped_homes
    with pytest.raises(ProposalError):
        _read_pull('fixture/owner', 1)
    assert captured == []


def test_routed_pr_read_pins_owner_and_github_hostname(scoped_homes):
    from hermes_cli.locked_skill_publication import _read_pull
    owner, captured = scoped_homes
    (owner / '.env').write_text('GH_TOKEN=fixture-owner-credential\n')
    assert _read_pull('fixture/owner', 1) == {}
    command, kwargs = captured[-1]
    env = kwargs['env']
    assert env['GH_TOKEN'] == 'fixture-owner-credential'
    assert env['GH_CONFIG_DIR'] == str(owner / '.config/gh')
    assert env['HERMES_HOME'] == str(owner)
    assert command[command.index('--hostname') + 1] == 'github.com'


@pytest.mark.parametrize('token_name', ['GH_TOKEN', 'GITHUB_TOKEN'])
def test_same_process_owner_preserves_environment_only_auth(scoped_homes, monkeypatch, token_name):
    from hermes_cli.locked_skill_publication import _read_pull
    import hermes_constants
    owner, captured = scoped_homes
    monkeypatch.setattr(hermes_constants, 'get_routing_process_hermes_home', lambda: owner)
    monkeypatch.delenv('GH_TOKEN', raising=False)
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    monkeypatch.setenv(token_name, 'fixture-own-process-credential')
    assert _read_pull('fixture/owner', 1) == {}
    assert captured[-1][1]['env'][token_name] == 'fixture-own-process-credential'
