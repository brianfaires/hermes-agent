from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_osv_scan_runs_in_forks_but_code_scanning_upload_is_upstream_only():
    workflow = (ROOT / ".github/workflows/osv-scanner.yml").read_text(encoding="utf-8")
    job_header = workflow.split("  scan:\n", 1)[1].split("    steps:\n", 1)[0]

    assert "github.repository" not in job_header
    upload = workflow.split("      - name: 'Upload to code-scanning'\n", 1)[1]
    upload_condition = upload.split("        uses:", 1)[0]
    assert "github.repository == 'NousResearch/hermes-agent'" in upload_condition
