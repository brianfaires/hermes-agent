"""A changed index budget belongs to a new conversation, including after compression (#167)."""
from types import SimpleNamespace

import pytest

from agent.system_prompt import _skills_prompt
from hermes_state import SessionDB

DESC = "Find primary evidence for a research question; compare competing sources and flag retractions"


def _agent(db, session_id):
    return SimpleNamespace(_session_db=db, session_id=session_id, _session_init_model_config={},
                           platform="cli", valid_tool_names={"skill_view"})


@pytest.mark.parametrize("initial, replacement", [(60, "full"), ("full", 60)])
def test_budget_survives_rebuild_resume_and_compression_child(tmp_path, monkeypatch, initial, replacement):
    skill = tmp_path / "skills" / "evidence"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f'---\nname: evidence\ndescription: "{DESC}"\n---\nBody.\n')
    config = tmp_path / "config.yaml"
    config.write_text(f"skills:\n  prompt_description_limit: {initial}\n")
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("original", source="cli")
        agent = _agent(db, "original")
        before = _skills_prompt(agent)
        assert ("flag retractions" in before) is (initial == "full")
        db.update_system_prompt("original", before)
        config.write_text(f"skills:\n  prompt_description_limit: {replacement}\n")

        from agent import conversation_compression as compression

        agent._cached_system_prompt = before
        agent._invalidate_system_prompt = lambda: setattr(agent, "_cached_system_prompt", None)
        agent._build_system_prompt = lambda _message: _skills_prompt(agent)
        monkeypatch.setattr(compression, "_refresh_agent_tool_definitions", lambda _agent: None)
        monkeypatch.setattr("agent.system_prompt.reconstruct_static_prefix", lambda *_a, **_kw: None)
        assert compression._rebuild_system_prompt_at_boundary(agent, "") == before
        assert _skills_prompt(_agent(db, "original")) == before

        db.publish_compression_child(
            parent_session_id="original", child_session_id="compacted", source="cli",
            messages=[{"role": "user", "content": "Summary"}], system_prompt=before,
            model_config=agent._session_init_model_config, require_compression_lease=False,
        )
        assert _skills_prompt(_agent(db, "compacted")) == before
        db.create_session("fresh", source="cli")
        after = _skills_prompt(_agent(db, "fresh"))
        assert ("flag retractions" in after) is (replacement == "full")


def test_stored_legacy_prompt_retains_default_budget(tmp_path):
    skill = tmp_path / "skills" / "evidence"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f'---\nname: evidence\ndescription: "{DESC}"\n---\nBody.\n')
    (tmp_path / "config.yaml").write_text("skills:\n  prompt_description_limit: full\n")
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("legacy", source="cli", system_prompt="Existing pre-feature prompt")
        prompt = _skills_prompt(_agent(db, "legacy"))
        assert DESC[:57] + "..." in prompt
        assert "flag retractions" not in prompt


def test_native_agent_pins_own_profile_before_prompt_rebuild(tmp_path, monkeypatch):
    ambient, owner = tmp_path / "ambient", tmp_path / "owner"
    ambient.mkdir()
    skill = owner / "skills/evidence"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f'---\nname: evidence\ndescription: "{DESC}"\n---\nBody.\n')
    (owner / "config.yaml").write_text("skills:\n  prompt_description_limit: full\n")
    monkeypatch.setenv("HERMES_HOME", str(ambient))
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture")
    from run_agent import AIAgent
    with SessionDB(db_path=owner / "state.db") as db:
        db.create_session("native", source="cli")
        agent = AIAgent(api_key="fixture", base_url="https://openrouter.ai/api/v1", model="test/model",
                        session_db=db, session_id="native", quiet_mode=True, skip_context_files=True, skip_memory=True)
        try:
            assert db.get_session_model_config_value("native", "skills_prompt_description_limit") == "full"
            (owner / "config.yaml").write_text("skills:\n  prompt_description_limit: 60\n")
            def failed_read(*_args):
                raise AssertionError("prompt rebuild must not access session storage")
            monkeypatch.setattr(db, "get_session", failed_read)
            assert "flag retractions" in _skills_prompt(agent)
        finally:
            agent.close()
