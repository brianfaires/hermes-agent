"""Disposable installer machines release disk while preserving diagnostic evidence."""
from types import SimpleNamespace

import pytest

from tests.e2e.core.windows_update import _machine as machines


@pytest.fixture
def isolated_machines(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_E2E_MACHINE_ROOT", str(tmp_path / "machines"))
    monkeypatch.setenv("HERMES_E2E_PROFILES_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("HERMES_E2E_ARTIFACTS", str(tmp_path / "artifacts"))
    monkeypatch.setattr(machines.Machine, "stage", lambda self: None)
    monkeypatch.setattr(machines.Machine, "kill_owned", lambda self: None)
    monkeypatch.setattr(machines.Machine, "evidence", lambda self: "synthetic evidence")
    monkeypatch.setattr(machines, "_restore_hkcu_path", lambda saved: None)
    return tmp_path


@pytest.mark.parametrize("evidence_location", ["external", "local", "inside-profile"])
def test_machine_teardown_preserves_evidence_and_removes_only_its_profile(
    isolated_machines, monkeypatch, evidence_location,
):
    root = isolated_machines
    machine = machines.new_machine(root, "http://localhost", label="owned")
    if evidence_location == "local":
        monkeypatch.delenv("HERMES_E2E_ARTIFACTS")
        retained = machine.logs
    else:
        artifacts = root / "artifacts" if evidence_location == "external" else machine.profile / "artifacts"
        monkeypatch.setenv("HERMES_E2E_ARTIFACTS", str(artifacts))
        retained = artifacts / machine.root.name / "transcripts"
    products = machine.hermes_home / "tools"
    products.mkdir(parents=True)
    (products / "product.bin").write_bytes(b"synthetic product")
    (machine.logs / "install.log").write_text("retained diagnosis")
    neighbour = root / "profiles" / "existing-user"
    neighbour.mkdir()
    (neighbour / "keep.txt").write_text("unrelated state")

    machine.teardown()

    assert machine.profile.exists() is (evidence_location != "external")
    assert (retained / "install.log").read_text() == "retained diagnosis"
    assert (neighbour / "keep.txt").read_text() == "unrelated state"


def test_machine_factory_refuses_a_preexisting_profile(isolated_machines, monkeypatch):
    root = isolated_machines
    monkeypatch.setattr(machines.uuid, "uuid4", lambda: SimpleNamespace(hex="abcd"))
    profile = root / "profiles" / "hermes-e2e-abcd"
    profile.mkdir(parents=True)
    (profile / "keep.txt").write_text("existing state")

    with pytest.raises(FileExistsError):
        machines.new_machine(root, "http://localhost", label="collision")
    assert (profile / "keep.txt").read_text() == "existing state"
