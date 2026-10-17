"""Home context files (config ``external_prompts`` and <home>/AGENTS.md) load
independently of working-directory discovery, scoped to the agent's own
profile home."""

import threading
from pathlib import Path
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
    for home in (root, profile):
        _configure(home, ["~/ORG.md"])
    return SimpleNamespace(root=root, profile=profile, workspace=workspace)


def _configure(home, entries):
    import yaml

    (home / "config.yaml").write_text(
        yaml.safe_dump({"external_prompts": entries}), encoding="utf-8"
    )


@pytest.fixture(autouse=True)
def _home_is_root(tmp_path, monkeypatch):
    # "~/ORG.md" in the fixture config resolves to <tmp>/hermes/ORG.md.
    monkeypatch.setenv("HOME", str(tmp_path / "hermes"))


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


def test_no_external_prompts_configured_loads_only_agents_md(layout):
    (layout.root / "ORG.md").write_text("ORG ROSTER", encoding="utf-8")
    (layout.profile / "AGENTS.md").write_text("ANG POLICY", encoding="utf-8")
    (layout.profile / "config.yaml").write_text("model: x\n", encoding="utf-8")

    out = prompt_builder.load_home_context_md(home_override=layout.profile)

    assert "ORG ROSTER" not in out
    assert "ANG POLICY" in out


def test_external_prompts_string_relative_and_hermes_home_token(layout):
    (layout.profile / "team.md").write_text("TEAM NOTES", encoding="utf-8")
    (layout.profile / "extra.md").write_text("EXTRA", encoding="utf-8")
    _configure(layout.profile, ["team.md", "$HERMES_HOME/extra.md", "missing.md"])

    out = prompt_builder.load_home_context_md(home_override=layout.profile)

    assert out.index("TEAM NOTES") < out.index("EXTRA")

    import yaml

    (layout.profile / "config.yaml").write_text(
        yaml.safe_dump({"external_prompts": "team.md"}), encoding="utf-8"
    )
    out = prompt_builder.load_home_context_md(home_override=layout.profile)
    assert "TEAM NOTES" in out and "EXTRA" not in out


def test_external_prompts_read_from_own_home_config_not_ambient(layout, monkeypatch):
    ops = layout.root / "profiles" / "ops"
    ops.mkdir()
    (layout.root / "OPS_ONLY.md").write_text("OPS ONLY", encoding="utf-8")
    _configure(ops, ["~/OPS_ONLY.md"])
    _configure(layout.profile, [])
    monkeypatch.setenv("HERMES_HOME", str(ops))

    assert prompt_builder.load_home_context_md(home_override=layout.profile) is None


def test_managed_prompts_override_user_with_process_env_expansion(layout, monkeypatch):
    managed = layout.root / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("HERMES_HOME", str(layout.root))
    _configure(layout.profile, ["user.md"])
    _configure(managed, ["${HERMES_HOME}/managed.md", "relative.md"])
    (layout.root / "managed.md").write_text("MANAGED NOTES", encoding="utf-8")
    (layout.profile / "user.md").write_text("USER NOTES", encoding="utf-8")
    (layout.profile / "relative.md").write_text("RELATIVE NOTES", encoding="utf-8")

    out = prompt_builder.load_home_context_md(home_override=layout.profile)

    assert out is not None
    assert "MANAGED NOTES" in out
    assert "USER NOTES" not in out
    assert out.index("MANAGED NOTES") < out.index("RELATIVE NOTES")


@pytest.mark.parametrize("invalid", [None, "external_prompts: [\n", "- not-a-mapping\n"])
def test_unavailable_user_config_warns_without_writes_or_stale_prompts(
    layout, monkeypatch, caplog, invalid,
):
    managed = layout.root / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _configure(layout.profile, ["old.md"])
    assert prompt_builder.home_context_paths(layout.profile)[0] == layout.profile / "old.md"

    config = layout.profile / "config.yaml"
    if invalid is None:
        config.unlink()
    else:
        config.write_text(invalid, encoding="utf-8")
    before = sorted(layout.profile.iterdir())
    with caplog.at_level("WARNING", logger="hermes_cli.config"):
        assert prompt_builder.home_context_paths(layout.profile) == [layout.profile / "AGENTS.md"]
    assert str(config) in caplog.text
    assert "user external_prompts omitted" in caplog.text
    assert sorted(layout.profile.iterdir()) == before
    if invalid is not None:
        assert config.read_text(encoding="utf-8") == invalid

    _configure(managed, ["managed.md"])
    assert prompt_builder.home_context_paths(layout.profile) == [
        layout.profile / "managed.md", layout.profile / "AGENTS.md",
    ]
    assert sorted(layout.profile.iterdir()) == before


@pytest.mark.parametrize("managed_source", [False, True])
def test_prompt_path_expansion_is_single_pass_in_thread(layout, monkeypatch, managed_source):
    managed = layout.root / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("HERMES_HOME", str(layout.root))
    monkeypatch.setenv("PROMPT_DIR", "${HERMES_HOME}/literal")
    # The replacement contains another token: do not reinterpret it.
    source = managed if managed_source else layout.profile
    _configure(source, ["${HERMES_HOME}/owner.md", "${PROMPT_DIR}/notes.md", "~/tilde.md"])
    result = []
    thread = threading.Thread(target=lambda: result.extend(
        prompt_builder.home_context_paths(layout.profile)
    ))
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    owner = layout.root if managed_source else layout.profile
    assert result == [
        owner / "owner.md",
        layout.profile / "${HERMES_HOME}" / "literal" / "notes.md",
        Path.home() / "tilde.md",
        layout.profile / "AGENTS.md",
    ]


@pytest.mark.parametrize("entries", [[], None, False, {}, ""])
def test_managed_empty_or_invalid_prompt_list_does_not_fall_back(layout, monkeypatch, entries):
    managed = layout.root / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _configure(layout.profile, ["user.md"])
    _configure(managed, entries)
    assert prompt_builder.home_context_paths(layout.profile) == [layout.profile / "AGENTS.md"]


def test_prompt_order_and_resolved_dedup_are_preserved(layout):
    _configure(layout.profile, ["team.md", "./team.md", "", None, "AGENTS.md", "last.md"])
    assert prompt_builder.home_context_paths(layout.profile) == [
        layout.profile / "team.md", layout.profile / "AGENTS.md", layout.profile / "last.md",
    ]
