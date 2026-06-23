# tests/hermes_cli/engteam/test_gates.py
import sys, tempfile
import pytest


@pytest.fixture()
def kb_home(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", tempfile.mkdtemp(prefix="engteam_gate_"))
    for mod in [m for m in sys.modules if m.startswith("hermes_cli")]:
        del sys.modules[mod]
    from hermes_cli import kanban_db
    from hermes_cli.engteam import constants
    kanban_db.create_board(slug=constants.ENG_BOARD, name="Engineering")
    return kanban_db


def _gate_with_child(kb):
    from hermes_cli.engteam.dag import build_stage_dag
    from hermes_cli.engteam.constants import GateSpec
    with kb.connect_closing(board="engineering") as conn:
        root = kb.create_task(conn, title="Project: g", assignee="lead",
                              created_by="eng-manager", board="engineering")
        g = build_stage_dag(conn, goal="g", root_id=root, lead="lead",
                            gates=[GateSpec(after_stage="review", kind="merge")])
        return g.gate_ids["merge"], g.stage_ids["commit"]


def test_blocked_gate_is_awaiting_user(kb_home):
    kb = kb_home
    from hermes_cli.engteam.gates import is_awaiting_user
    gate_id, _ = _gate_with_child(kb)
    with kb.connect_closing(board="engineering") as conn:
        assert is_awaiting_user(conn, gate_id) is True


def test_resolve_gate_completes_and_releases_child(kb_home):
    kb = kb_home
    from hermes_cli.engteam.gates import resolve_gate, is_awaiting_user
    gate_id, commit_id = _gate_with_child(kb)
    with kb.connect_closing(board="engineering") as conn:
        assert resolve_gate(conn, gate_id, approver="brian", note="ship it") is True
        assert kb.get_task(conn, gate_id).status == "done"
        assert is_awaiting_user(conn, gate_id) is False


def test_resolve_non_blocked_gate_returns_false(kb_home):
    kb = kb_home
    from hermes_cli.engteam.gates import resolve_gate
    gate_id, _ = _gate_with_child(kb)
    with kb.connect_closing(board="engineering") as conn:
        resolve_gate(conn, gate_id, approver="brian")
        assert resolve_gate(conn, gate_id, approver="brian") is False
