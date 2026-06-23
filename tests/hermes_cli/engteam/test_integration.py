# tests/hermes_cli/engteam/test_integration.py
import sys, tempfile
import pytest


@pytest.fixture()
def kb_home(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", tempfile.mkdtemp(prefix="engteam_int_"))
    for mod in [m for m in sys.modules if m.startswith("hermes_cli")
                or m.startswith("gateway")]:
        del sys.modules[mod]
    from hermes_cli import kanban_db
    from hermes_cli.engteam import constants
    kanban_db.create_board(slug=constants.ENG_BOARD, name="Engineering")
    return kanban_db


def test_handoff_yields_a_well_formed_stage_dag(kb_home):
    kb = kb_home
    from gateway.engteam_handoff import handoff_engineering
    from hermes_cli.engteam import registry, constants

    ack = handoff_engineering("add CSV export to the reports endpoint")
    assert "engineering" in ack.lower()

    live = registry.list_live_projects()
    assert len(live) == 1
    proj = live[0]
    assert "CSV export" in proj.goal

    with kb.connect_closing(board="engineering") as conn:
        # root is the completed blackboard
        assert kb.get_task(conn, proj.root_id).status == "done"
        # first stage is ready, downstream stages wait
        children = kb.child_ids(conn, proj.root_id)
        assert children, "spec stage should be linked under root"
        spec = kb.get_task(conn, children[0])
        assert spec.assignee == constants.STAGE_SPECS["spec"].profile
        assert spec.status == "ready"
