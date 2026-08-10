from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_runs_on_staging_pushes() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text())
    triggers = workflow.get("on", workflow.get(True))

    push_branches = triggers["push"]["branches"]

    assert "staging" in push_branches
