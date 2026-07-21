from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_contributor_check_uses_the_actual_pull_request_base():
    workflow = (ROOT / ".github/workflows/contributor-check.yml").read_text(
        encoding="utf-8"
    )

    assert "github.base_ref || 'main'" in workflow
    assert 'git merge-base "origin/${BASE_REF}" HEAD' in workflow
