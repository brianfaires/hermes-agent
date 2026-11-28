"""Explicit completion carries legacy SDKs at its first PM worker request."""

import json
import os
from pathlib import Path
import sys

import pytest

from hermes_cli import gitlock, update_completion, update_lock, venv_sync
from pm import client, environments, extras
from pm.lock import Facts


@pytest.fixture
def completion(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    site = root / "venv" / "Lib" / "site-packages"
    for anchor in ("telegram", "discord", "mcp"):
        (site / anchor).mkdir(parents=True)
    request, result = tmp_path / "request.json", tmp_path / "result.json"
    request.write_text(json.dumps({
        "schema": 1, "source": str(root), "receipt": {"update_id": "selection-test"},
        "bytecode_cache": str(tmp_path / "bytecode"),
    }))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(update_completion, "__file__", str(root / "hermes_cli/update_completion.py"))
    monkeypatch.setattr(sys, "argv", ["update_completion.py", str(request), str(result)])
    # main owns these process globals; restore them at fixture teardown.
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(client, "ensure_tools_for_sync", lambda: None)
    monkeypatch.setattr(gitlock, "convert_treeless_checkout_first", lambda root: None)
    monkeypatch.setattr(update_lock, "_acquire_checkout", lambda root: None)
    monkeypatch.setattr(venv_sync, "collect_superseded_generations", lambda root: None)
    monkeypatch.setattr(environments, "project_python", lambda root: Path(sys.executable))
    monkeypatch.setattr(environments, "activation_environment", lambda root: dict(os.environ))
    requests = []
    # Observe the real PM client's IPC boundary; no resolver or install is run.
    monkeypatch.setattr(client, "_request", lambda operation, data, **kw: requests.append((operation, data)))

    def prepared_child(*args, **kwargs):
        result.write_text("{}")
        return 0

    monkeypatch.setattr(update_completion.subprocess, "call", prepared_child)
    return root, requests


@pytest.mark.parametrize("declined, expected", [
    ([], ["all", "computer-use", "discord", "telegram"]),
    (["telegram"], ["all", "computer-use", "discord"]),
])
def test_first_completion_sync_carries_installed_sdks_without_undoing_declines(completion, declined, expected):
    root, requests = completion
    extras.record_declined_extras(add=declined, project_root=root)

    assert update_completion.main() == 0

    operation, selection = requests[0]
    assert operation == "sync_venv"
    assert selection["extras"] == expected
    assert selection["carried"] == expected[1:]
    assert extras.declined_extras(root) == frozenset(declined)


def test_established_completion_sync_leaves_selection_to_recorded_pm_environment(completion):
    root, requests = completion
    Facts(environments.runtime_facts_path(root)).record_state("venv", "recorded", ["all", "fal"])

    assert update_completion.main() == 0

    operation, selection = requests[0]
    assert operation == "sync_venv"
    assert selection["extras"] is None
    assert not selection["carried"]
