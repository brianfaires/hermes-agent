# tests/hermes_cli/engteam/test_review_loop.py
import sys, tempfile
import pytest


@pytest.fixture()
def kb_home(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", tempfile.mkdtemp(prefix="engteam_loop_"))
    monkeypatch.setenv("HERMES_ENGTEAM_MAX_REVIEW_ROUNDS", "2")
    for mod in [m for m in sys.modules if m.startswith("hermes_cli")]:
        del sys.modules[mod]
    from hermes_cli import kanban_db
    from hermes_cli.engteam import constants
    kanban_db.create_board(slug=constants.ENG_BOARD, name="Engineering")
    return kanban_db


def _graph(kb):
    from hermes_cli.engteam.dag import build_stage_dag
    with kb.connect_closing(board="engineering") as conn:
        root = kb.create_task(conn, title="P", assignee="lead",
                              created_by="eng-manager", board="engineering")
        return build_stage_dag(conn, goal="P", root_id=root, lead="lead")


def test_iteration_spawns_dev_card_with_findings(kb_home):
    kb = kb_home
    from hermes_cli.engteam.review_loop import open_review_iteration
    g = _graph(kb)
    with kb.connect_closing(board="engineering") as conn:
        new_dev = open_review_iteration(
            conn, root_id=g.root_id, review_id=g.stage_ids["review"],
            findings="missing tests for edge case X")
        assert new_dev is not None
        task = kb.get_task(conn, new_dev)
        assert task.assignee == "developer"
        assert "missing tests" in (task.body or "")
        assert kb.parent_ids(conn, new_dev) == [g.stage_ids["review"]]


def test_loop_is_bounded_then_returns_none(kb_home):
    kb = kb_home
    from hermes_cli.engteam.review_loop import open_review_iteration, rounds_used
    g = _graph(kb)
    with kb.connect_closing(board="engineering") as conn:
        assert open_review_iteration(conn, root_id=g.root_id,
                                     review_id=g.stage_ids["review"], findings="r1")
        assert open_review_iteration(conn, root_id=g.root_id,
                                     review_id=g.stage_ids["review"], findings="r2")
        # MAX=2 exhausted -> escalate signal
        assert open_review_iteration(conn, root_id=g.root_id,
                                     review_id=g.stage_ids["review"], findings="r3") is None
        assert rounds_used(conn, g.root_id) == 2
