"""Persistent eng-manager registry of live projects.

A project is a root card on the engineering board that has no parents and a
completion metadata marker `kind == "engteam_project"`. Listing/searching is a
board query — there is no separate state store."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from hermes_cli import kanban_db as kb
from hermes_cli.engteam.constants import DEFAULT_STAGES, ENG_BOARD, GateSpec
from hermes_cli.engteam.dag import build_stage_dag

_LIVE_STATUSES = ("todo", "ready", "running", "blocked", "review", "done")


@dataclass(frozen=True)
class Project:
    root_id: str
    goal: str
    lead: str
    status: str


def open_project(
    *,
    goal: str,
    lead: str = "lead",
    stages: Sequence[str] = DEFAULT_STAGES,
    gates: Sequence[GateSpec] = (),
    created_by: str = "eng-manager",
    idempotency_key: str | None = None,
) -> Project:
    with kb.connect_closing(board=ENG_BOARD) as conn:
        root_id = kb.create_task(
            conn,
            title=f"Project: {goal}"[:200],
            body=f"Engineering project root / blackboard.\n\nGoal: {goal}",
            assignee=lead,
            created_by=created_by,
            board=ENG_BOARD,
            idempotency_key=idempotency_key,
        )
        # Idempotency: an existing root already has its DAG; don't rebuild.
        if not kb.child_ids(conn, root_id):
            build_stage_dag(conn, goal=goal, root_id=root_id, lead=lead,
                            stages=stages, gates=gates, created_by=created_by)
    return Project(root_id=root_id, goal=goal, lead=lead, status="open")


def _is_project_root(conn, task) -> bool:
    if kb.parent_ids(conn, task.id):
        return False
    return (task.title or "").startswith("Project: ")


def _goal_of(task) -> str:
    title = task.title or ""
    return title[len("Project: "):] if title.startswith("Project: ") else title


def list_live_projects() -> list[Project]:
    out: list[Project] = []
    with kb.connect_closing(board=ENG_BOARD) as conn:
        seen: set[str] = set()
        for status in _LIVE_STATUSES:
            for task in kb.list_tasks(conn, status=status):
                if task.id in seen or not _is_project_root(conn, task):
                    continue
                seen.add(task.id)
                out.append(Project(task.id, _goal_of(task),
                                   task.assignee or "lead", task.status))
    return out


def find_project(query: str) -> Project | None:
    q = (query or "").strip().lower()
    if not q:
        return None
    projects = list_live_projects()
    for p in projects:
        if p.root_id == query:
            return p
    for p in projects:
        if q in p.goal.lower():
            return p
    return None
