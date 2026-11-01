"""``skills.prompt_description_limit``: the index keeps a trigger past char 60 when configured."""
import pytest

from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache
from hermes_cli.config import validate_config_structure

DESC = "Find primary evidence for a research question; compare competing sources and flag retractions"


def _skills_dir(tmp_path):
    d = tmp_path / "skills" / "research" / "evidence"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: evidence\ndescription: \"{DESC}\"\n---\n\nBody.\n")
    return tmp_path / "skills"


def _set_limit(value, home, *, clear_cache=True):
    # skills_dir_override resolves the profile home as the skills dir's parent: config lives there.
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("" if value is None else f"skills:\n  prompt_description_limit: {value}\n")
    if clear_cache:
        clear_skills_system_prompt_cache()


def _index(tmp_path):
    return build_skills_system_prompt(available_tools={"skill_view"}, skills_dir_override=_skills_dir(tmp_path))


def test_default_budget_truncates_at_60(tmp_path):
    _set_limit(None, tmp_path)
    prompt = _index(tmp_path)
    assert DESC[:57] + "..." in prompt and "flag retractions" not in prompt


@pytest.mark.parametrize("value", ["full", "200"])
def test_configured_budget_keeps_trigger_past_60(tmp_path, value):
    _set_limit(value, tmp_path)
    assert "flag retractions" in _index(tmp_path)


def test_raising_budget_is_not_masked_by_cached_snapshot(tmp_path):
    _set_limit(None, tmp_path)
    assert "flag retractions" not in _index(tmp_path)  # writes the snapshot
    _set_limit("full", tmp_path, clear_cache=False)
    assert "flag retractions" in _index(tmp_path)  # warm process cache
    clear_skills_system_prompt_cache()
    assert "flag retractions" in _index(tmp_path)  # cold process, existing snapshot
    _set_limit(None, tmp_path, clear_cache=False)
    assert "flag retractions" not in _index(tmp_path)


def test_coercion_and_fitting():
    from agent.skill_utils import coerce_prompt_description_limit, fit_skill_description

    assert coerce_prompt_description_limit("full") is None
    assert coerce_prompt_description_limit(120) == 120
    for bad in (5, "lots", True, 0, None, 3.5, [], {}):
        with pytest.raises(ValueError):
            coerce_prompt_description_limit(bad)
    assert fit_skill_description(DESC, None) == DESC
    assert fit_skill_description(DESC, 60) == DESC[:57] + "..."


def test_invalid_value_is_a_config_error_and_falls_back(tmp_path):
    issues = validate_config_structure({"skills": {"prompt_description_limit": 5}})
    assert any("prompt_description_limit" in i.message and i.severity == "error" for i in issues)
    assert not validate_config_structure({"skills": {"prompt_description_limit": "full"}})
    _set_limit(5, tmp_path)
    assert DESC[:57] + "..." in _index(tmp_path)


def test_mounted_shared_root_uses_owning_profile_budget(tmp_path):
    shared = tmp_path / "shared-skills" / "evidence"
    shared.mkdir(parents=True)
    (shared / "SKILL.md").write_text(f'---\nname: evidence\ndescription: "{DESC}"\n---\nBody.\n')
    homes = [tmp_path / "bounded", tmp_path / "full"]
    for home, limit in zip(homes, [60, "full"]):
        home.mkdir()
        (home / "skills").mkdir()
        (home / "config.yaml").write_text(
            f'skills:\n  prompt_description_limit: {limit}\n  external_dirs: ["{shared.parent}"]\n'
        )
    # Alternate homes in one process: the shared root and its files are identical.
    for home, full in [(homes[0], False), (homes[1], True), (homes[0], False)]:
        prompt = build_skills_system_prompt(available_tools={"skill_view"},
                                            skills_dir_override=home / "skills")
        assert "evidence" in prompt
        assert ("flag retractions" in prompt) is full
        if not full:
            assert DESC[:57] + "..." in prompt
