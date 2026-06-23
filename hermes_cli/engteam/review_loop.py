"""Bounded Dev<>Review iteration. Each failed review spawns one fresh dev
card carrying the findings; after MAX_REVIEW_ROUNDS the caller escalates."""
from __future__ import annotations

import os

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_swarm
from hermes_cli.engteam.constants import ENG_BOARD, STAGE_SPECS

MAX_REVIEW_ROUNDS = max(1, int(os.getenv("HERMES_ENGTEAM_MAX_REVIEW_ROUNDS", "3")))

_ROUND_KEY = "review_rounds"


def rounds_used(conn, root_id: str) -> int:
    value = kanban_swarm.latest_blackboard(conn, root_id).get(_ROUND_KEY)
    return int(value) if isinstance(value, int) else 0


def open_review_iteration(conn, *, root_id: str, review_id: str, findings: str,
                          created_by: str = "lead") -> str | None:
    used = rounds_used(conn, root_id)
    if used >= MAX_REVIEW_ROUNDS:
        return None
    dev_spec = STAGE_SPECS["dev"]
    new_dev = kb.create_task(
        conn,
        title=f"[dev:fix round {used + 1}]"[:200],
        body=dev_spec.body + f"\n\n## Review findings to address\n{findings}",
        assignee=dev_spec.profile,
        created_by=created_by,
        parents=[review_id],
        board=ENG_BOARD,
        workspace_kind=dev_spec.workspace_kind,
        skills=list(dev_spec.skills),
    )
    kanban_swarm.post_blackboard_update(
        conn, root_id, author=created_by, key=_ROUND_KEY, value=used + 1,
    )
    return new_dev
