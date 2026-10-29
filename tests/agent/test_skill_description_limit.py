"""``skills.prompt_description_limit``: the index keeps a trigger past char 60 when configured."""
import pytest

from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache
from agent.skill_utils import coerce_prompt_description_limit, fit_skill_description
from hermes_cli.config import validate_config_structure

DESC = "Find primary evidence for a research question; compare competing sources and flag retractions"


def _skills_dir(tmp_path):
    d = tmp_path / "skills" / "research" / "evidence"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: evidence\ndescription: \"{DESC}\"\n---\n\nBody.\n")
    return tmp_path / "skills"


def _set_limit(value, home):
    # skills_dir_override resolves the profile home as the skills dir's parent: config lives there.
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("" if value is None else f"skills:\n  prompt_description_limit: {value}\n")
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
    _set_limit("full", tmp_path)
    assert "flag retractions" in _index(tmp_path)
    _set_limit(None, tmp_path)
    assert "flag retractions" not in _index(tmp_path)


def test_coercion_and_fitting():
    assert coerce_prompt_description_limit("full") is None
    assert coerce_prompt_description_limit(120) == 120
    for bad in (5, "lots", True, 0):
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
