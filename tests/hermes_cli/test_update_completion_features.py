"""Source-update completion preserves features from a legacy venv (#217)."""

from pathlib import Path
import sys

import pytest

import pm
from pm import client, environments, extras
from hermes_cli import gitlock, update_completion, update_lock, venv_sync


@pytest.mark.parametrize("recorded", [False, True], ids=["legacy", "recorded"])
def test_completion_prepares_the_preserved_feature_selection(tmp_path, monkeypatch, recorded):
    root = tmp_path / "checkout"
    site = root / "venv/lib/python3.14/site-packages"
    for name in ("telegram", "discord"):
        (site / name).mkdir(parents=True)
    facts = tmp_path / "facts.json"
    if recorded:
        facts.write_text('{"packages":{"venv":{"extras":["google"]}}}')
    monkeypatch.setattr(environments, "runtime_facts_path", lambda source: facts)
    monkeypatch.setattr(extras, "_PLATFORM_GATES", {})
    monkeypatch.setattr(venv_sync, "arm_completion", lambda source: None)
    monkeypatch.setattr(venv_sync, "collect_superseded_generations", lambda source: None)
    monkeypatch.setattr(gitlock, "convert_treeless_checkout_first", lambda source: None)
    monkeypatch.setattr(client, "ensure_tools_for_sync", lambda: None)
    monkeypatch.setattr(environments, "project_python", lambda source: Path(sys.executable))
    monkeypatch.setattr(environments, "activation_environment", lambda source: {})
    monkeypatch.setattr(update_lock, "_acquire_checkout", lambda source: None)
    monkeypatch.setattr(update_lock, "checkout_lock_fds", lambda source: ())
    monkeypatch.setattr(update_completion.subprocess, "call", lambda *args, **kwargs: 0)
    selections = []
    monkeypatch.setattr(pm, "sync_venv", lambda **kwargs: selections.append(kwargs))
    result = tmp_path / "result.json"
    result.write_text("{}")
    request = {"source": str(root), "receipt": {"update_id": "owned-features"},
               "bytecode_cache": str(tmp_path / "bytecode")}

    assert update_completion._prepare(request, tmp_path / "request.json", result) == 0
    assert len(selections) == 1
    selection = selections[0]
    assert selection["explicit"] and selection["evict_incompatible_plugins"]
    assert selection["project_root"] == root
    if recorded:
        assert selection.get("extras") is None, "Recorded extras remain PM's authority"
    else:
        assert selection.get("extras") == ["all", "discord", "telegram"]
