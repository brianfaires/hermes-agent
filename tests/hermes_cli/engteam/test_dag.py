# tests/hermes_cli/engteam/test_dag.py
import sys, tempfile
import pytest


@pytest.fixture()
def kb_home(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", tempfile.mkdtemp(prefix="engteam_dag_"))
    for mod in [m for m in sys.modules if m.startswith("hermes_cli")]:
        del sys.modules[mod]
    from hermes_cli import kanban_db
    from hermes_cli.engteam import constants
    kanban_db.create_board(slug=constants.ENG_BOARD, name="Engineering")
    return kanban_db


def _root(kb):
    with kb.connect_closing(board="engineering") as conn:
        return kb.create_task(conn, title="Project: widget", assignee="lead",
                              created_by="eng-manager", board="engineering")


def test_builds_linear_chain_with_dependencies(kb_home):
    kb = kb_home
    from hermes_cli.engteam.dag import build_stage_dag
    root = _root(kb)
    with kb.connect_closing(board="engineering") as conn:
        g = build_stage_dag(conn, goal="widget", root_id=root, lead="lead")
        # root completed as blackboard
        assert kb.get_task(conn, root).status == "done"
        # spec ready (its only parent, root, is done); plan waits on spec
        assert kb.get_task(conn, g.stage_ids["spec"]).status == "ready"
        assert kb.get_task(conn, g.stage_ids["plan"]).status == "todo"
        assert kb.parent_ids(conn, g.stage_ids["plan"]) == [g.stage_ids["spec"]]
        # judgment card waits on the last stage (commit)
        assert kb.parent_ids(conn, g.judgment_id) == [g.stage_ids["commit"]]
        assert kb.get_task(conn, g.judgment_id).assignee == "lead"


def test_stage_cards_carry_profile_and_skills(kb_home):
    kb = kb_home
    from hermes_cli.engteam.dag import build_stage_dag
    from hermes_cli.engteam import constants
    root = _root(kb)
    with kb.connect_closing(board="engineering") as conn:
        g = build_stage_dag(conn, goal="widget", root_id=root, lead="lead")
        dev = kb.get_task(conn, g.stage_ids["dev"])
        assert dev.assignee == "developer"
        assert dev.workspace_kind == "worktree"
        assert list(dev.skills) == list(constants.STAGE_SPECS["dev"].skills)


def test_gate_inserts_blocked_card_between_stages(kb_home):
    kb = kb_home
    from hermes_cli.engteam.dag import build_stage_dag
    from hermes_cli.engteam.constants import GateSpec
    root = _root(kb)
    with kb.connect_closing(board="engineering") as conn:
        g = build_stage_dag(conn, goal="widget", root_id=root, lead="lead",
                            gates=[GateSpec(after_stage="review", kind="merge")])
        gate_id = g.gate_ids["merge"]
        assert kb.get_task(conn, gate_id).status == "blocked"
        # commit now depends on the gate, not directly on review
        assert kb.parent_ids(conn, g.stage_ids["commit"]) == [gate_id]
        assert kb.parent_ids(conn, gate_id) == [g.stage_ids["review"]]


def test_subset_of_stages_is_honored(kb_home):
    kb = kb_home
    from hermes_cli.engteam.dag import build_stage_dag
    root = _root(kb)
    with kb.connect_closing(board="engineering") as conn:
        g = build_stage_dag(conn, goal="tiny", root_id=root, lead="lead",
                            stages=["spec", "dev", "review"])
        assert set(g.stage_ids) == {"spec", "dev", "review"}
        assert kb.parent_ids(conn, g.stage_ids["dev"]) == [g.stage_ids["spec"]]
