# tests/hermes_cli/engteam/test_registry.py
import sys, tempfile
import pytest


@pytest.fixture()
def kb_home(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", tempfile.mkdtemp(prefix="engteam_reg_"))
    for mod in [m for m in sys.modules if m.startswith("hermes_cli")]:
        del sys.modules[mod]
    from hermes_cli import kanban_db
    from hermes_cli.engteam import constants
    kanban_db.create_board(slug=constants.ENG_BOARD, name="Engineering")
    return kanban_db


def test_open_project_creates_root_and_dag(kb_home):
    kb = kb_home
    from hermes_cli.engteam import registry
    proj = registry.open_project(goal="add dark mode")
    with kb.connect_closing(board="engineering") as conn:
        root = kb.get_task(conn, proj.root_id)
        assert root is not None
        assert kb.child_ids(conn, proj.root_id)  # spec card linked under root
    assert proj.goal == "add dark mode"


def test_list_live_projects_excludes_other_cards(kb_home):
    kb = kb_home
    from hermes_cli.engteam import registry
    registry.open_project(goal="alpha")
    registry.open_project(goal="beta")
    live = registry.list_live_projects()
    goals = sorted(p.goal for p in live)
    assert goals == ["alpha", "beta"]


def test_find_project_by_substring(kb_home):
    from hermes_cli.engteam import registry
    registry.open_project(goal="ship the widget exporter")
    found = registry.find_project("widget")
    assert found is not None and "widget" in found.goal
    assert registry.find_project("nonexistent") is None


def test_open_project_is_idempotent(kb_home):
    from hermes_cli.engteam import registry
    a = registry.open_project(goal="dupe", idempotency_key="k1")
    b = registry.open_project(goal="dupe", idempotency_key="k1")
    assert a.root_id == b.root_id
    assert len(registry.list_live_projects()) == 1
