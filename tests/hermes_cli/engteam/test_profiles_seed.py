# tests/hermes_cli/engteam/test_profiles_seed.py
import sys, tempfile
import pytest


@pytest.fixture()
def hermes_home(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", tempfile.mkdtemp(prefix="engteam_prof_"))
    for mod in [m for m in sys.modules if m.startswith("hermes_cli")]:
        del sys.modules[mod]
    from hermes_cli import profiles
    return profiles


def test_every_profile_has_a_prompt_and_description(hermes_home):
    from hermes_cli.engteam import profiles_seed, constants
    for name in constants.PROFILES:
        assert name in profiles_seed.PROFILE_DESCRIPTIONS
        assert profiles_seed.PROFILE_DESCRIPTIONS[name].strip()
        assert profiles_seed.prompt_path(name).exists()


def test_install_creates_all_profiles_idempotently(hermes_home):
    profiles = hermes_home
    from hermes_cli.engteam import profiles_seed, constants
    created = profiles_seed.install_engteam_profiles()
    assert sorted(created) == sorted(constants.PROFILES)
    for name in constants.PROFILES:
        assert profiles.profile_exists(name)
    # Second run is a no-op.
    assert profiles_seed.install_engteam_profiles() == []
