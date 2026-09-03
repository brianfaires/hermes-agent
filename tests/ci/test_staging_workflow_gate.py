from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yaml"


def test_ci_orchestrator_runs_on_main_and_staging_pushes() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))

    assert triggers["push"]["branches"] == ["main", "staging"]
