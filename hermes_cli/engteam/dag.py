"""Build the Spec->Plan->Dev<>Review->Commit stage-DAG for one project.

Wiring follows the swarm model: the root card is completed immediately and
serves as the shared blackboard; downstream stage cards are chained with
explicit parents so each becomes `ready` only when its predecessor is `done`.
A terminal judgment card lets the lead wake when the pipeline finishes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_swarm
from hermes_cli.engteam.constants import (
    DEFAULT_STAGES, ENG_BOARD, GateSpec, STAGE_SPECS,
)


@dataclass(frozen=True)
class EngTeamGraph:
    root_id: str
    stage_ids: dict[str, str]
    gate_ids: dict[str, str]
    judgment_id: str


def build_stage_dag(
    conn,
    *,
    goal: str,
    root_id: str,
    lead: str,
    stages: Sequence[str] = DEFAULT_STAGES,
    gates: Sequence[GateSpec] = (),
    created_by: str = "lead",
) -> EngTeamGraph:
    unknown = [s for s in stages if s not in STAGE_SPECS]
    if unknown:
        raise ValueError(f"unknown stage(s): {unknown}")

    gates_by_stage: dict[str, GateSpec] = {g.after_stage: g for g in gates}

    # Root becomes the blackboard. complete_task accepts a `running` task.
    kb.complete_task(
        conn, root_id,
        summary="Engineering pipeline planned; root is the shared blackboard.",
        metadata={"kind": "engteam_project", "goal": goal, "stages": list(stages)},
    )
    kanban_swarm.post_blackboard_update(
        conn, root_id, author=created_by, key="topology",
        value={"goal": goal, "stages": list(stages)},
    )

    stage_ids: dict[str, str] = {}
    gate_ids: dict[str, str] = {}
    prev: list[str] = [root_id]

    for name in stages:
        spec = STAGE_SPECS[name]
        sid = kb.create_task(
            conn,
            title=f"[{name}] {goal}"[:200],
            body=spec.body,
            assignee=spec.profile,
            created_by=created_by,
            parents=prev,
            board=ENG_BOARD,
            workspace_kind=spec.workspace_kind,
            skills=list(spec.skills),
        )
        stage_ids[name] = sid
        prev = [sid]

        gate = gates_by_stage.get(name)
        if gate is not None:
            # Parallel STICKY gate. Parent is the already-done root, so the
            # card is `ready` at creation; block_task then makes the block
            # STICKY. This is load-bearing: recompute_ready auto-promotes an
            # ordinary dependency-blocked card to `ready` once its parents
            # complete, so a gate merely chained behind a stage would silently
            # open when that stage finished. recompute_ready skips
            # sticky-blocked cards, so only unblock_task (user approval)
            # releases it -- the gate never proceeds silently.
            gid = kb.create_task(
                conn,
                title=f"[gate:{gate.kind}] {goal}"[:200],
                body=f"{gate.kind} gate. Stays blocked until the user approves; "
                     f"the next stage waits on both this gate and the prior stage.",
                assignee=gate.assignee,
                created_by=created_by,
                parents=[root_id],
                board=ENG_BOARD,
            )
            kb.block_task(conn, gid, reason=f"awaiting user {gate.kind} approval")
            gate_ids[gate.kind] = gid
            # The next stage waits on BOTH the gated stage and the gate.
            prev = [sid, gid]

    judgment_id = kb.create_task(
        conn,
        title=f"[judge] {goal}"[:200],
        body="Pipeline complete. Judge done-ness: if work remains, add cards; "
             "otherwise mark the project complete and notify the eng-manager.",
        assignee=lead,
        created_by=created_by,
        parents=prev,
        board=ENG_BOARD,
    )

    return EngTeamGraph(root_id, stage_ids, gate_ids, judgment_id)
