"""Home context files (<root>/ORG.md and <home>/AGENTS.md) load independently
of working-directory discovery, scoped to the agent's own profile home."""

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import prompt_builder
from agent.system_prompt import build_system_prompt_parts


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / "hermes"
    profile = root / "profiles" / "ang"
    profile.mkdir(parents=True)
    workspace = tmp_path / "elsewhere" / "repo"
    workspace.mkdir(parents=True)
    return SimpleNamespace(root=root, profile=profile, workspace=workspace)


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
        _emit_status=lambda *_args, **_kwargs: None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _parts(agent, cwd):
    with (
        patch("run_agent.load_soul_md", return_value="SOUL"),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("agent.system_prompt.resolve_context_cwd", return_value=cwd),
    ):
        return build_system_prompt_parts(agent)


def test_profile_loads_root_org_and_own_agents_md(layout):
    (layout.root / "ORG.md").write_text("ORG ROSTER", encoding="utf-8")
    (layout.profile / "AGENTS.md").write_text("ANG POLICY", encoding="utf-8")
    (layout.root / "AGENTS.md").write_text("DEFAULT POLICY", encoding="utf-8")

    out = prompt_builder.load_home_context_md(home_override=layout.profile)

    assert "ORG ROSTER" in out
    assert "ANG POLICY" in out
    assert "DEFAULT POLICY" not in out
    assert out.index("ORG ROSTER") < out.index("ANG POLICY")


def test_default_home_loads_org_once_and_own_agents_md(layout):
    (layout.root / "ORG.md").write_text("ORG ROSTER", encoding="utf-8")
    (layout.root / "AGENTS.md").write_text("DEFAULT POLICY", encoding="utf-8")

    out = prompt_builder.load_home_context_md(home_override=layout.root)

    assert out.count("ORG ROSTER") == 1
    assert "DEFAULT POLICY" in out


def test_absent_or_empty_files_return_none(layout):
    (layout.profile / "AGENTS.md").write_text("   \n", encoding="utf-8")
    assert prompt_builder.load_home_context_md(home_override=layout.profile) is None


def test_scoped_to_home_override_not_ambient(layout, monkeypatch):
    other = layout.root / "profiles" / "ops"
    other.mkdir()
    (other / "AGENTS.md").write_text("OPS POLICY", encoding="utf-8")
    (layout.profile / "AGENTS.md").write_text("ANG POLICY", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(other))

    result = {}
    t = threading.Thread(
        target=lambda: result.update(
            out=prompt_builder.load_home_context_md(home_override=layout.profile)
        )
    )
    t.start()
    t.join()

    assert "ANG POLICY" in result["out"]
    assert "OPS POLICY" not in result["out"]


def test_oversized_file_is_truncated_with_read_pointer(layout):
    (layout.profile / "AGENTS.md").write_text("x" * 50_000, encoding="utf-8")
    with patch.object(prompt_builder, "_get_context_file_max_chars", return_value=1_000):
        out = prompt_builder.load_home_context_md(home_override=layout.profile)
    assert "truncated" in out
    assert str(layout.profile / "AGENTS.md") in out
    assert len(out) < 2_000


def test_system_prompt_injects_home_context_with_unrelated_cwd(layout, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(layout.profile))
    (layout.root / "ORG.md").write_text("ORG ROSTER", encoding="utf-8")
    (layout.profile / "AGENTS.md").write_text("ANG POLICY", encoding="utf-8")
    (layout.workspace / "AGENTS.md").write_text("REPO RULES", encoding="utf-8")

    parts = _parts(_make_agent(), layout.workspace)

    assert "ORG ROSTER" in parts["stable"]
    assert "ANG POLICY" in parts["stable"]
    assert "REPO RULES" in parts["context"]


def test_cron_style_session_gets_home_context(layout, monkeypatch):
    # Cron loads the SOUL identity but skips cwd project context.
    monkeypatch.setenv("HERMES_HOME", str(layout.profile))
    (layout.root / "ORG.md").write_text("ORG ROSTER", encoding="utf-8")
    (layout.workspace / "AGENTS.md").write_text("REPO RULES", encoding="utf-8")

    parts = _parts(
        _make_agent(load_soul_identity=True, skip_context_files=True),
        layout.workspace,
    )

    assert "ORG ROSTER" in parts["stable"]
    assert "REPO RULES" not in parts["context"]


def test_cwd_at_profile_home_injects_agents_md_once(layout, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(layout.profile))
    (layout.profile / "AGENTS.md").write_text("ANG POLICY", encoding="utf-8")

    parts = _parts(_make_agent(), layout.profile)

    combined = parts["stable"] + parts["context"]
    assert combined.count("ANG POLICY") == 1


def test_fully_skipped_session_gets_no_home_context(layout, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(layout.profile))
    (layout.root / "ORG.md").write_text("ORG ROSTER", encoding="utf-8")

    parts = _parts(_make_agent(skip_context_files=True), layout.workspace)

    assert "ORG ROSTER" not in parts["stable"]
