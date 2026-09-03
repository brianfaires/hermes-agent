"""Behavior tests for the explicit-path incident evidence closure utility."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "close_incident_evidence.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("close_incident_evidence", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def closure_tool():
    return _load_module()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _closed_incident(tmp_path: Path) -> tuple[Path, Path]:
    incident = tmp_path / "INC-2026-001"
    incident.mkdir()
    for name, content in {
        "closure-report.md": "Closed after verified recovery.\n",
        "reproducer.md": "Steps to reproduce.\n",
        "patch.diff": "diff --git a/x b/x\n",
        "references.md": "Reference evidence.\n",
    }.items():
        (incident / name).write_text(content, encoding="utf-8")
    database = incident / "state.sqlite"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE evidence (value TEXT)")
        conn.execute("INSERT INTO evidence VALUES ('preserved')")

    artifacts = [
        {
            "path": name,
            "kind": kind,
            "sha256": _sha256(incident / name),
            "disposition": "retain",
        }
        for name, kind in (
            ("closure-report.md", "closure_report"),
            ("reproducer.md", "reproducer"),
            ("patch.diff", "patch"),
            ("references.md", "reference"),
        )
    ]
    artifacts.append(
        {
            "path": "state.sqlite",
            "kind": "full_state_sqlite",
            "sha256": _sha256(database),
            "disposition": "compact",
        }
    )
    manifest = {
        "schema_version": 1,
        "incident": {
            "id": "INC-2026-001",
            "status": "closed",
            "closed_at": "2026-08-10T10:00:00Z",
            "closure_report": "closure-report.md",
            "retention_policy_version": "2026-08-v1",
        },
        "live_access": {"holders": [], "leases": []},
        "artifacts": artifacts,
    }
    manifest_path = incident / "incident-evidence.json"
    _write_json(manifest_path, manifest)
    return incident, manifest_path


@pytest.mark.parametrize("status", ["open", "active", "investigating", "unknown"])
def test_refuses_non_closed_incident_statuses(closure_tool, tmp_path, status):
    incident, manifest_path = _closed_incident(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["incident"]["status"] = status
    _write_json(manifest_path, manifest)

    result = closure_tool.close_incident(incident)

    assert result["ok"] is False
    assert any(f"status={status!r}" in error for error in result["errors"])


def test_refuses_unmanifested_sqlite_snapshot(closure_tool, tmp_path):
    incident, _ = _closed_incident(tmp_path)
    (incident / "unmanifested.sqlite").write_bytes(b"SQLite format 3\x00")

    result = closure_tool.close_incident(incident)

    assert result["ok"] is False
    assert "unmanifested SQLite snapshot: unmanifested.sqlite" in result["errors"]


def test_refuses_live_access_holders(closure_tool, tmp_path):
    incident, manifest_path = _closed_incident(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["live_access"]["holders"] = ["recovery-worker-7"]
    _write_json(manifest_path, manifest)

    result = closure_tool.close_incident(incident)

    assert result["ok"] is False
    assert "live access holders remain: recovery-worker-7" in result["errors"]


def test_refuses_live_access_lease_holders(closure_tool, tmp_path):
    incident, manifest_path = _closed_incident(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["live_access"]["leases"] = ["forensic-shell-3"]
    _write_json(manifest_path, manifest)

    result = closure_tool.close_incident(incident)

    assert result["ok"] is False
    assert result["errors"] == ["live access leases remain: forensic-shell-3"]


def test_refuses_missing_live_access_lease_inventory(closure_tool, tmp_path):
    incident, manifest_path = _closed_incident(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["live_access"].pop("leases")
    _write_json(manifest_path, manifest)

    result = closure_tool.close_incident(incident)

    assert result["ok"] is False
    assert result["errors"] == ["live_access.leases is required"]


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("closed_at", "", "incident.closed_at is required"),
        ("closure_report", "", "incident.closure_report is required"),
        ("retention_policy_version", "", "incident.retention_policy_version is required"),
    ],
)
def test_refuses_missing_closure_metadata(closure_tool, tmp_path, field, value, expected):
    incident, manifest_path = _closed_incident(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["incident"][field] = value
    _write_json(manifest_path, manifest)

    result = closure_tool.close_incident(incident)

    assert result["ok"] is False
    assert expected in result["errors"]


def test_refuses_missing_required_evidence_or_checksum(closure_tool, tmp_path):
    incident, manifest_path = _closed_incident(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"] = [
        artifact for artifact in manifest["artifacts"] if artifact["kind"] != "patch"
    ]
    next(artifact for artifact in manifest["artifacts"] if artifact["kind"] == "reproducer").pop("sha256")
    _write_json(manifest_path, manifest)

    result = closure_tool.close_incident(incident)

    assert result["ok"] is False
    assert "required evidence kind is missing: patch" in result["errors"]
    assert "artifact reproducer.md is missing sha256" in result["errors"]


def test_closed_incident_with_no_live_access_is_dry_run_by_default(closure_tool, tmp_path):
    incident, _ = _closed_incident(tmp_path)
    before = (incident / "state.sqlite").read_bytes()

    result = closure_tool.close_incident(incident)

    assert result == {
        "ok": True,
        "applied": False,
        "actions": [{"action": "compact", "path": "state.sqlite"}],
        "errors": [],
    }
    assert (incident / "state.sqlite").read_bytes() == before


def test_compacts_only_manifest_declared_snapshot_after_explicit_confirmation(closure_tool, tmp_path):
    incident, _ = _closed_incident(tmp_path)

    result = closure_tool.close_incident(incident, apply=True, confirm_closed=True)

    assert result["ok"] is True
    assert result["applied"] is True
    with sqlite3.connect(incident / "state.sqlite") as conn:
        assert conn.execute("SELECT value FROM evidence").fetchone() == ("preserved",)


def test_apply_requires_explicit_closed_confirmation(closure_tool, tmp_path):
    incident, _ = _closed_incident(tmp_path)

    result = closure_tool.close_incident(incident, apply=True)

    assert result["ok"] is False
    assert result["errors"] == ["--apply requires --confirm-closed"]
