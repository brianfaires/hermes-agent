"""Single source of truth for engineering-team names, the stage->specialist->
skill mapping, and shared markers. Nothing here touches the DB."""
from __future__ import annotations

from dataclasses import dataclass

ENG_BOARD = "engineering"
BLACKBOARD_PROJECT_KEY = "engteam_project"

PROFILES = ("eng-manager", "lead", "spec-writer", "planner", "developer", "reviewer")
DEFAULT_STAGES = ("spec", "plan", "dev", "review", "commit")


@dataclass(frozen=True)
class StageSpec:
    name: str
    profile: str
    skills: tuple[str, ...]
    workspace_kind: str = "scratch"
    body: str = ""


@dataclass(frozen=True)
class GateSpec:
    after_stage: str
    kind: str
    assignee: str = "eng-manager"


STAGE_SPECS: dict[str, StageSpec] = {
    "spec": StageSpec(
        "spec", "spec-writer", ("superpowers:brainstorming",),
        body="Run the brainstorming skill on this project. Produce a draft spec and "
             "a decision list, tagging each decision auto (clear best option) or "
             "needs-user. Post needs-user items to the root blackboard for the lead.",
    ),
    "plan": StageSpec(
        "plan", "planner", ("superpowers:writing-plans",),
        body="Turn the approved spec into an implementation plan via the "
             "writing-plans skill.",
    ),
    "dev": StageSpec(
        "dev", "developer",
        ("superpowers:test-driven-development", "superpowers:executing-plans",
         "superpowers:using-git-worktrees"),
        workspace_kind="worktree",
        body="Implement the plan with TDD in this worktree. On completion run "
             "requesting-code-review and hand off to the reviewer.",
    ),
    "review": StageSpec(
        "review", "reviewer", ("code-review",),
        body="Review the developer's branch independently. Complete with metadata "
             '{"gate":"pass"} or {"gate":"fail","findings":"..."}.',
    ),
    "commit": StageSpec(
        "commit", "developer", ("superpowers:finishing-a-development-branch",),
        body="On review pass (and merge sign-off if gated) land the branch via "
             "finishing-a-development-branch.",
    ),
}
