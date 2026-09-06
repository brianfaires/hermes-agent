"""Tests for kb.decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACES_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None, tenant=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        tenant=tenant,
        triage=True,
    )


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "kanban@example.com"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Kanban Test"],
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
        text=True,
    )


def _parents_of(conn, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (task_id,),
    ).fetchall()
    return [row["parent_id"] for row in rows]


def _table_snapshot(conn) -> dict[str, list[tuple]]:
    tables = ("tasks", "task_links", "task_comments", "task_events")
    snapshot = {}
    for table in tables:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        snapshot[table] = [tuple(row) for row in rows]
    return snapshot


def test_decompose_creates_children_and_promotes_root(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="ship a feature")
        assert kb.get_task(conn, tid).status == "triage"

    children = [
        {"title": "research", "body": "look at prior art", "assignee": "researcher", "parents": []},
        {"title": "build it", "body": "write code", "assignee": "engineer", "parents": [0]},
    ]
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=children,
            author="decomposer",
        )
    assert child_ids is not None
    assert len(child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])

    # Root flipped to todo with orchestrator assignee, gated by children.
    assert root.status == "todo"
    assert root.assignee == "orchestrator"
    # First child has no internal parents → ready on recompute_ready.
    assert c0.status == "ready"
    assert c0.assignee == "researcher"
    # Second child has parents=[0] → stays in todo until c0 completes.
    assert c1.status == "todo"
    assert c1.assignee == "engineer"


def test_decompose_returns_none_when_task_missing(kanban_home):
    with kb.connect() as conn:
        result = kb.decompose_triage_task(
            conn,
            "nonexistent",
            root_assignee="orch",
            children=[{"title": "x"}],
            author="me",
        )
    assert result is None


def test_decompose_returns_none_when_task_not_in_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="already a real task")  # not triage
        result = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "x"}],
            author="me",
        )
    assert result is None


def test_decompose_empty_children_returns_none(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        result = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[],
            author="me",
        )
    assert result is None


def test_decompose_rejects_self_parent(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="cannot list itself"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[{"title": "x", "parents": [0]}],
                author="me",
            )


def test_decompose_rejects_out_of_range_parent(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="not a valid index"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[{"title": "x", "parents": [5]}],
                author="me",
            )


def test_decompose_rejects_cyclic_parents(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="cyclic dependency"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[
                    {"title": "A", "parents": [1]},
                    {"title": "B", "parents": [0]},
                ],
                author="me",
            )


def test_decompose_records_audit_comment_and_event(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            author="alice",
        )
    assert child_ids is not None

    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)

    assert any("Decomposed into" in (c.body or "") for c in comments)
    assert any(ev.kind == "decomposed" for ev in events)


def test_decompose_propagates_blocked_external_parent_to_runnable_children(kanban_home):
    with kb.connect() as conn:
        prereq = kb.create_task(conn, title="blocked prerequisite", assignee="worker")
        tid = kb.create_task(
            conn,
            title="blocked triage root",
            assignee="worker",
            parents=[prereq],
            triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=[{"title": "first runnable child", "assignee": "worker"}],
            author="decomposer",
        )
        assert child_ids is not None
        child = child_ids[0]

        child_parents = _parents_of(conn, child)
        assert prereq in child_parents
        assert kb.get_task(conn, child).status == "todo"
        assert kb.claim_task(conn, child, claimer="host:worker") is None

        linked_events = [
            ev.payload
            for ev in kb.list_events(conn, child)
            if ev.kind == "linked" and ev.payload
        ]
        assert any(
            ev.get("parent") == prereq
            and ev.get("child") == child
            and ev.get("propagated_from") == tid
            for ev in linked_events
        )


def test_decompose_propagates_multiple_prerequisites_only_to_child_roots(kanban_home):
    with kb.connect() as conn:
        prereq_a = kb.create_task(conn, title="prereq a", assignee="worker")
        prereq_b = kb.create_task(conn, title="prereq b", assignee="worker")
        tid = kb.create_task(
            conn,
            title="root",
            assignee="worker",
            parents=[prereq_a, prereq_b],
            triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=[
                {"title": "root child a"},
                {"title": "nested child", "parents": [0]},
                {"title": "root child b"},
            ],
            author="decomposer",
        )
        assert child_ids is not None

        assert set(_parents_of(conn, child_ids[0])) == {prereq_a, prereq_b}
        assert set(_parents_of(conn, child_ids[2])) == {prereq_a, prereq_b}
        assert _parents_of(conn, child_ids[1]) == [child_ids[0]]
        assert kb.get_task(conn, child_ids[0]).status == "todo"
        assert kb.get_task(conn, child_ids[2]).status == "todo"

        kb.claim_task(conn, prereq_a)
        kb.complete_task(conn, prereq_a)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child_ids[0]).status == "todo"

        kb.claim_task(conn, prereq_b)
        kb.complete_task(conn, prereq_b)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child_ids[0]).status == "ready"
        assert kb.get_task(conn, child_ids[2]).status == "ready"
        assert kb.get_task(conn, child_ids[1]).status == "todo"

        kb.claim_task(conn, child_ids[0])
        kb.complete_task(conn, child_ids[0])
        assert kb.get_task(conn, child_ids[1]).status == "ready"


def test_decompose_dependency_propagation_survives_reopen_and_retry(kanban_home):
    with kb.connect() as conn:
        prereq = kb.create_task(conn, title="prereq", assignee="worker")
        tid = kb.create_task(
            conn,
            title="root",
            assignee="worker",
            parents=[prereq],
            triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=[{"title": "child", "assignee": "worker"}],
            author="decomposer",
        )
        child = child_ids[0]

    with kb.connect() as conn:
        assert kb.claim_task(conn, child, claimer="host:first") is None
        assert kb.get_task(conn, child).status == "todo"

    with kb.connect() as conn:
        kb.claim_task(conn, prereq)
        kb.complete_task(conn, prereq, result="ok")

    with kb.connect() as conn:
        kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, child, claimer="host:retry")
        assert claimed is not None
        assert claimed.status == "running"


def test_decompose_second_wave_propagates_parent_dependencies(kanban_home):
    with kb.connect() as conn:
        first_wave = kb.create_task(conn, title="first wave", assignee="worker")
        second_wave = kb.create_task(
            conn,
            title="second wave triage",
            assignee="worker",
            parents=[first_wave],
            triage=True,
        )
        grandchild_ids = kb.decompose_triage_task(
            conn,
            second_wave,
            root_assignee="orchestrator",
            children=[
                {"title": "grandchild root"},
                {"title": "grandchild dependent", "parents": [0]},
            ],
            author="decomposer",
        )
        assert grandchild_ids is not None
        assert _parents_of(conn, grandchild_ids[0]) == [first_wave]
        assert _parents_of(conn, grandchild_ids[1]) == [grandchild_ids[0]]
        assert kb.get_task(conn, grandchild_ids[0]).status == "todo"


def test_decompose_rejects_unsafe_dir_workspace_inheritance(kanban_home, tmp_path):
    """Implicit dir fan-out would put multiple children in the same project dir."""
    proj = tmp_path / "myproject"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="codegen root", assignee="worker",
            workspace_kind="dir", workspace_path=str(proj), triage=True,
        )
        with pytest.raises(ValueError, match="cannot inherit dir workspace"):
            kb.decompose_triage_task(
                conn, tid, root_assignee="orchestrator",
                children=[{"title": "part A"}, {"title": "part B"}],
                author="decomposer",
            )

    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "triage"
        rows = conn.execute(
            "SELECT id FROM tasks WHERE title IN ('part A', 'part B')"
        ).fetchall()
        assert rows == []


def test_decompose_children_stay_scratch_when_root_scratch(kanban_home):
    """No regression: a scratch root still fans out into scratch children."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="scratch root", assignee="worker",
            workspace_kind="scratch", triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{"title": "s1"}], author="decomposer",
        )
    with kb.connect() as conn:
        t = kb.get_task(conn, child_ids[0])
    assert t.workspace_kind == "scratch"
    assert t.workspace_path is None


def test_decompose_does_not_inherit_root_scratch_path(kanban_home, tmp_path):
    """Scratch children stay independently allocated, even after root materialization."""
    root_workspace = tmp_path / "root-scratch"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="scratch root",
            assignee="worker",
            workspace_kind="scratch",
            workspace_path=str(root_workspace),
            triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=[{"title": "s1"}],
            author="decomposer",
        )
    with kb.connect() as conn:
        t = kb.get_task(conn, child_ids[0])
    assert t.workspace_kind == "scratch"
    assert t.workspace_path is None


def test_decompose_worktree_children_keep_repo_root_anchor_contract(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="worktree root",
            assignee="worker",
            workspace_kind="worktree",
            workspace_path=str(repo),
            triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=[{"title": "part A"}, {"title": "part B"}],
            author="decomposer",
        )
        child = kb.get_task(conn, child_ids[0])
        assert child.workspace_kind == "worktree"
        assert child.workspace_path == str(repo)
        resolved = kb.resolve_workspace(child)

    assert resolved == repo / ".worktrees" / child_ids[0]
    assert resolved.exists()


def test_decompose_worktree_children_do_not_reuse_root_linked_checkout(
    kanban_home, tmp_path,
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="claimed worktree root",
            assignee="worker",
            workspace_kind="worktree",
            workspace_path=str(repo),
            triage=True,
        )
        root = kb.get_task(conn, tid)
        root_workspace = kb.resolve_workspace(root)
        kb.set_workspace_path(conn, tid, root_workspace)

        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=[{"title": "part A"}],
            author="decomposer",
        )
        child = kb.get_task(conn, child_ids[0])
        assert child.workspace_kind == "worktree"
        assert child.workspace_path == str(repo)
        child_workspace = kb.resolve_workspace(child)

    assert root_workspace == repo / ".worktrees" / tid
    assert child_workspace == repo / ".worktrees" / child_ids[0]
    assert child_workspace != root_workspace


def test_decompose_per_child_workspace_override(kanban_home):
    """An explicit per-child workspace beats inheritance."""
    proj = "/home/teknium/myproject"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="root", assignee="worker",
            workspace_kind="dir", workspace_path=proj, triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[
                {"title": "override", "workspace_kind": "dir",
                 "workspace_path": "/other/repo"},
                {"title": "independent scratch", "workspace_kind": "scratch"},
            ],
            author="decomposer",
        )
    with kb.connect() as conn:
        over = kb.get_task(conn, child_ids[0])
        scratch = kb.get_task(conn, child_ids[1])
    assert over.workspace_path == "/other/repo"
    assert scratch.workspace_kind == "scratch"
    assert scratch.workspace_path is None


def test_decompose_rejects_explicit_workspace_collisions_atomically(
    kanban_home, tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    same_project_alias = project / ".." / "project"
    scratch = tmp_path / "scratch"
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="explicit collision root",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(project),
            triage=True,
        )
        before = _table_snapshot(conn)

        with pytest.raises(ValueError, match="workspace.*collides"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orchestrator",
                children=[
                    {
                        "title": "dir root alias",
                        "workspace_kind": "dir",
                        "workspace_path": str(same_project_alias),
                    },
                    {
                        "title": "other dir",
                        "workspace_kind": "dir",
                        "workspace_path": str(tmp_path / "other"),
                    },
                ],
                author="decomposer",
            )
        assert _table_snapshot(conn) == before

        with pytest.raises(ValueError, match="workspace.*collides"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orchestrator",
                children=[
                    {
                        "title": "descendant dir",
                        "workspace_kind": "dir",
                        "workspace_path": str(project / "nested"),
                    },
                ],
                author="decomposer",
            )
        assert _table_snapshot(conn) == before

        with pytest.raises(ValueError, match="workspace.*collides"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orchestrator",
                children=[
                    {
                        "title": "scratch explicit",
                        "workspace_kind": "scratch",
                        "workspace_path": str(scratch),
                    },
                    {
                        "title": "scratch alias",
                        "workspace_kind": "scratch",
                        "workspace_path": str(scratch / ".." / "scratch"),
                    },
                ],
                author="decomposer",
            )
        assert _table_snapshot(conn) == before

        concrete_worktree = repo / ".worktrees" / "shared"
        with pytest.raises(ValueError, match="workspace.*collides"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orchestrator",
                children=[
                    {
                        "title": "worktree target",
                        "workspace_kind": "worktree",
                        "workspace_path": str(concrete_worktree),
                    },
                    {
                        "title": "worktree target alias",
                        "workspace_kind": "worktree",
                        "workspace_path": str(concrete_worktree / ".." / "shared"),
                    },
                ],
                author="decomposer",
            )
        assert _table_snapshot(conn) == before

        linked_worktree = repo / ".worktrees" / "linked"
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                "-b",
                "linked-branch",
                str(linked_worktree),
                "main",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        before = _table_snapshot(conn)
        with pytest.raises(ValueError, match="workspace.*collides"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orchestrator",
                children=[
                    {
                        "title": "linked checkout",
                        "workspace_kind": "worktree",
                        "workspace_path": str(linked_worktree),
                    },
                    {
                        "title": "linked checkout alias",
                        "workspace_kind": "worktree",
                        "workspace_path": str(linked_worktree / ".." / "linked"),
                    },
                ],
                author="decomposer",
            )
        assert _table_snapshot(conn) == before


def test_decompose_rejects_worktree_anchor_inside_explicit_dir_atomically(
    kanban_home, tmp_path,
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    with kb.connect() as conn:
        root = kb.create_task(conn, title="mixed collision", triage=True, assignee="worker")
        before = _table_snapshot(conn)

        with pytest.raises(ValueError, match="workspace.*collides"):
            kb.decompose_triage_task(
                conn,
                root,
                root_assignee="worker",
                children=[
                    {
                        "title": "allocated",
                        "assignee": "worker",
                        "workspace_kind": "worktree",
                        "workspace_path": str(repo),
                    },
                    {
                        "title": "explicit",
                        "assignee": "worker",
                        "workspace_kind": "dir",
                        "workspace_path": str(repo / ".worktrees"),
                    },
                ],
            )

        assert _table_snapshot(conn) == before
        assert kb.get_task(conn, root).status == "triage"
        assert conn.execute(
            "SELECT 1 FROM tasks WHERE title IN ('allocated', 'explicit')"
        ).fetchone() is None
        assert not (repo / ".worktrees").exists()


def test_decompose_rejects_board_default_worktree_inside_explicit_dir_atomically(
    kanban_home, tmp_path,
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    kb.create_board("mixed-default", default_workdir=str(repo))
    kb.set_current_board("mixed-default")

    with kb.connect(board="mixed-default") as conn:
        root = kb.create_task(conn, title="mixed default", triage=True, assignee="worker")
        before = _table_snapshot(conn)

        with pytest.raises(ValueError, match="workspace.*collides"):
            kb.decompose_triage_task(
                conn,
                root,
                root_assignee="worker",
                children=[
                    {
                        "title": "allocated default",
                        "assignee": "worker",
                        "workspace_kind": "worktree",
                    },
                    {
                        "title": "explicit parent",
                        "assignee": "worker",
                        "workspace_kind": "dir",
                        "workspace_path": str(repo / ".worktrees"),
                    },
                ],
            )

        assert _table_snapshot(conn) == before
        assert conn.execute(
            "SELECT 1 FROM tasks WHERE title IN ('allocated default', 'explicit parent')"
        ).fetchone() is None
        assert not (repo / ".worktrees").exists()


def test_decompose_explicit_board_allows_implicit_worktree_when_current_differs(
    kanban_home, tmp_path,
):
    repo = tmp_path / "selected-repo"
    _init_git_repo(repo)
    kb.create_board("selected", default_workdir=str(repo))
    assert kb.get_current_board() == "default"

    with kb.connect(board="selected") as conn:
        root = kb.create_task(conn, title="explicit board", triage=True, assignee="worker")
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="worker",
            children=[
                {
                    "title": "independent implicit worktree",
                    "assignee": "worker",
                    "workspace_kind": "worktree",
                },
            ],
        )
        child = kb.get_task(conn, child_ids[0])
        assert child.workspace_kind == "worktree"
        assert child.workspace_path is None
        assert kb.resolve_workspace(child, board="selected") == repo / ".worktrees" / child_ids[0]


def test_decompose_explicit_board_predicts_scratch_destination_when_current_differs(
    kanban_home,
):
    kb.create_board("selected")
    assert kb.get_current_board() == "default"

    with kb.connect(board="selected") as conn:
        root = kb.create_task(conn, title="scratch board", triage=True, assignee="worker")
        before = _table_snapshot(conn)

        with pytest.raises(ValueError, match="workspace.*collides"):
            kb.decompose_triage_task(
                conn,
                root,
                root_assignee="worker",
                children=[
                    {
                        "title": "explicit selected scratch root",
                        "assignee": "worker",
                        "workspace_kind": "dir",
                        "workspace_path": str(kb.workspaces_root(board="selected")),
                    },
                    {
                        "title": "implicit selected scratch child",
                        "assignee": "worker",
                        "workspace_kind": "scratch",
                    },
                ],
            )

        assert _table_snapshot(conn) == before
        assert conn.execute(
            "SELECT 1 FROM tasks WHERE title IN "
            "('explicit selected scratch root', 'implicit selected scratch child')"
        ).fetchone() is None
        assert not kb.workspaces_root(board="selected").exists()


def test_decompose_explicit_board_predicts_implicit_worktree_collision_when_current_differs(
    kanban_home, tmp_path,
):
    selected_repo = tmp_path / "selected-repo"
    current_repo = tmp_path / "current-repo"
    _init_git_repo(selected_repo)
    _init_git_repo(current_repo)
    kb.write_board_metadata("default", default_workdir=str(current_repo))
    kb.create_board("selected", default_workdir=str(selected_repo))
    assert kb.get_current_board() == "default"

    with kb.connect(board="selected") as conn:
        root = kb.create_task(conn, title="mixed explicit board", triage=True, assignee="worker")
        before = _table_snapshot(conn)

        with pytest.raises(ValueError, match="workspace.*collides"):
            kb.decompose_triage_task(
                conn,
                root,
                root_assignee="worker",
                children=[
                    {
                        "title": "implicit selected worktree",
                        "assignee": "worker",
                        "workspace_kind": "worktree",
                    },
                    {
                        "title": "explicit selected worktrees dir",
                        "assignee": "worker",
                        "workspace_kind": "dir",
                        "workspace_path": str(selected_repo / ".worktrees"),
                    },
                ],
            )

        assert _table_snapshot(conn) == before
        assert conn.execute(
            "SELECT 1 FROM tasks WHERE title IN "
            "('implicit selected worktree', 'explicit selected worktrees dir')"
        ).fetchone() is None
        assert not (selected_repo / ".worktrees").exists()


def test_decompose_rejects_implicit_scratch_inside_explicit_dir_atomically(
    kanban_home,
):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="scratch collision", triage=True, assignee="worker")
        before = _table_snapshot(conn)

        with pytest.raises(ValueError, match="workspace.*collides"):
            kb.decompose_triage_task(
                conn,
                root,
                root_assignee="worker",
                children=[
                    {"title": "explicit parent", "workspace_kind": "dir",
                     "workspace_path": str(kb.workspaces_root())},
                    {"title": "reserved", "workspace_kind": "scratch"},
                ],
            )

        assert _table_snapshot(conn) == before
        assert conn.execute(
            "SELECT 1 FROM tasks WHERE title IN ('explicit parent', 'reserved')"
        ).fetchone() is None
        assert not kb.workspaces_root().exists()


def test_decompose_allows_sibling_worktree_repo_anchors(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="root", assignee="worker", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=[
                {
                    "title": "part A",
                    "workspace_kind": "worktree",
                    "workspace_path": str(repo),
                },
                {
                    "title": "part B",
                    "workspace_kind": "worktree",
                    "workspace_path": str(repo / "."),
                },
            ],
            author="decomposer",
        )

        first = kb.get_task(conn, child_ids[0])
        second = kb.get_task(conn, child_ids[1])
        assert kb.resolve_workspace(first) == repo / ".worktrees" / child_ids[0]
        assert kb.resolve_workspace(second) == repo / ".worktrees" / child_ids[1]


@pytest.mark.parametrize(
    ("child", "message"),
    [
        ({"title": "bad kind", "workspace_kind": "cloud"}, "workspace_kind"),
        ({"title": "none kind", "workspace_kind": None}, "workspace_kind"),
        ({"title": "false kind", "workspace_kind": False}, "workspace_kind"),
        ({"title": "list kind", "workspace_kind": []}, "workspace_kind"),
        (
            {"title": "empty path", "workspace_kind": "scratch", "workspace_path": ""},
            "workspace_path",
        ),
        (
            {"title": "none path", "workspace_kind": "scratch", "workspace_path": None},
            "workspace_path",
        ),
        (
            {"title": "false path", "workspace_kind": "scratch", "workspace_path": False},
            "workspace_path",
        ),
        (
            {"title": "relative path", "workspace_kind": "dir", "workspace_path": "rel"},
            "absolute",
        ),
        (
            {"title": "missing path", "workspace_kind": "dir"},
            "workspace_path",
        ),
        (
            {"title": "bad branch", "workspace_kind": "scratch", "branch_name": "wt/nope"},
            "persistent",
        ),
        (
            {"title": "invalid branch", "workspace_kind": "worktree", "branch_name": "bad branch"},
            "branch_name",
        ),
        (
            {"title": "false branch", "workspace_kind": "worktree", "branch_name": False},
            "branch_name",
        ),
    ],
)
def test_decompose_rejects_malformed_child_workspace_inputs(
    kanban_home, child, message,
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="root", assignee="worker", triage=True)
        with pytest.raises(ValueError, match=message):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orchestrator",
                children=[child],
                author="decomposer",
            )
        assert kb.get_task(conn, tid).status == "triage"
        assert conn.execute(
            "SELECT 1 FROM tasks WHERE title = ?",
            (child["title"],),
        ).fetchone() is None


def test_decompose_rolls_back_atomically_when_insert_audit_fails(
    kanban_home, monkeypatch,
):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="root", assignee="worker", triage=True)

        original_append_event = kb._append_event

        def fail_child_created(conn_, task_id, kind, payload=None, *, run_id=None):
            if kind == "created" and payload and payload.get("from_decompose_of") == tid:
                raise RuntimeError("injected audit failure")
            return original_append_event(
                conn_, task_id, kind, payload, run_id=run_id,
            )

        monkeypatch.setattr(kb, "_append_event", fail_child_created)
        before = _table_snapshot(conn)
        with pytest.raises(RuntimeError, match="injected audit failure"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orchestrator",
                children=[{"title": "child"}],
                author="decomposer",
            )

        assert _table_snapshot(conn) == before
        assert kb.get_task(conn, tid).status == "triage"
        assert conn.execute(
            "SELECT 1 FROM tasks WHERE title = 'child'"
        ).fetchone() is None


def test_decompose_nested_retry_and_completion_under_blocked_parent(kanban_home):
    with kb.connect() as conn:
        external = kb.create_task(conn, title="external gate", assignee="worker")
        root = kb.create_task(
            conn,
            title="root triage",
            assignee="worker",
            parents=[external],
            triage=True,
        )
        first_wave = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "generated triage candidate", "assignee": "worker"},
                {"title": "generated sibling", "assignee": "worker"},
            ],
            author="decomposer",
        )
        assert first_wave is not None

    with kb.connect() as conn:
        assert all(
            kb.claim_task(conn, tid, claimer="blocked:first") is None
            for tid in first_wave
        )
        assert kb.promote_task(
            conn, first_wave[0], actor="test", force=True,
        ) == (True, None)
        assert kb.block_task(
            conn, first_wave[0], reason="needs decomposition", kind="needs_input",
        )
        assert kb.unblock_task(conn, first_wave[0])
        assert kb.promote_task(
            conn, first_wave[0], actor="test", force=True,
        ) == (True, None)
        assert kb.block_task(
            conn, first_wave[0], reason="needs decomposition", kind="needs_input",
        )
        assert kb.get_task(conn, first_wave[0]).status == "triage"
        nested = kb.decompose_triage_task(
            conn,
            first_wave[0],
            root_assignee="nested-orchestrator",
            children=[
                {"title": "nested entry A", "assignee": "worker"},
                {"title": "nested entry B", "assignee": "worker"},
                {
                    "title": "nested dependent",
                    "parents": [0, 1],
                    "assignee": "worker",
                },
            ],
            author="decomposer",
        )
        assert nested is not None
        entrypoints = [nested[0], nested[1], first_wave[1]]
        assert all(
            kb.claim_task(conn, tid, claimer="blocked:nested") is None
            for tid in entrypoints
        )

    with kb.connect() as conn:
        kb.claim_task(conn, external, claimer="worker:gate")
        assert kb.complete_task(conn, external, result="gate complete")

    with kb.connect() as conn:
        kb.recompute_ready(conn)
        claimed_a = kb.claim_task(conn, nested[0], claimer="worker:a")
        assert claimed_a is not None
        assert kb.claim_task(conn, nested[2], claimer="worker:too-early") is None
        assert kb.reclaim_task(conn, nested[0], reason="exercise supported retry")

    with kb.connect() as conn:
        retried_a = kb.claim_task(conn, nested[0], claimer="worker:a-retry")
        assert retried_a is not None
        assert kb.complete_task(conn, nested[0], result="a done")
        claimed_b = kb.claim_task(conn, nested[1], claimer="worker:b")
        assert claimed_b is not None
        assert kb.complete_task(conn, nested[1], result="b done")
        assert kb.claim_task(conn, nested[2], claimer="worker:dependent") is not None
        assert kb.complete_task(conn, nested[2], result="dependent done")
        assert (
            kb.claim_task(conn, first_wave[0], claimer="worker:nested-root")
            is not None
        )
        assert kb.complete_task(conn, first_wave[0], result="nested root complete")
        assert kb.claim_task(conn, first_wave[1], claimer="worker:sibling") is not None
        assert kb.complete_task(conn, first_wave[1], result="sibling complete")
        assert kb.get_task(conn, root).status == "ready"


def test_rejected_shared_dir_decompose_is_repeatably_ineligible(
    kanban_home, tmp_path,
):
    shared = tmp_path / "shared"
    with kb.connect() as conn:
        external = kb.create_task(conn, title="external gate", assignee="worker")
        root = kb.create_task(
            conn,
            title="root",
            assignee="worker",
            parents=[external],
            workspace_kind="dir",
            workspace_path=str(shared),
            triage=True,
        )
        for _ in range(2):
            before = _table_snapshot(conn)
            with pytest.raises(ValueError, match="cannot inherit dir workspace"):
                kb.decompose_triage_task(
                    conn,
                    root,
                    root_assignee="orchestrator",
                    children=[{"title": "child A"}, {"title": "child B"}],
                    author="decomposer",
                )
            assert _table_snapshot(conn) == before
            kb.recompute_ready(conn)
            assert kb.get_task(conn, root).status == "triage"
            assert kb.claim_task(conn, root, claimer="worker:root") is None
            assert conn.execute(
                "SELECT 1 FROM tasks WHERE title IN ('child A', 'child B')"
            ).fetchone() is None
