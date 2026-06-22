from hermes_cli.engteam import constants as c


def test_stage_specs_cover_default_stages():
    for name in c.DEFAULT_STAGES:
        assert name in c.STAGE_SPECS
        spec = c.STAGE_SPECS[name]
        assert spec.name == name
        assert spec.profile in c.PROFILES
        assert isinstance(spec.skills, tuple) and spec.skills


def test_dev_runs_in_a_worktree():
    assert c.STAGE_SPECS["dev"].workspace_kind == "worktree"


def test_review_profile_differs_from_dev():
    assert c.STAGE_SPECS["review"].profile != c.STAGE_SPECS["dev"].profile


def test_profiles_roster_is_the_full_team():
    assert set(c.PROFILES) == {
        "eng-manager", "lead", "spec-writer", "planner", "developer", "reviewer",
    }
