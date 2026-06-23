"""Seed the six engineering-team profiles (idempotent).

Descriptions are what the kanban decomposer's profile-routing matches on, so
they describe ROLE, not just name. Prompts ship as files under prompts/."""
from __future__ import annotations

from pathlib import Path

from hermes_cli import profiles as profiles_mod
from hermes_cli.engteam.constants import PROFILES

_PROMPT_DIR = Path(__file__).parent / "prompts"

PROFILE_DESCRIPTIONS: dict[str, str] = {
    "eng-manager": "Persistent engineering intake/router and the user's stable "
                   "engineering contact. Opens projects, tracks live work, routes "
                   "the user to the right per-project lead, relays milestones.",
    "lead": "Per-project orchestrator. Decides stages, negotiates gates in the "
            "Spec Q&A, routes stages to specialists, judges done-ness. Does not "
            "write code or specs itself.",
    "spec-writer": "Turns a request into a spec using the brainstorming skill; "
                   "flags decisions that need the user.",
    "planner": "Turns an approved spec into a bite-sized TDD implementation plan "
               "using the writing-plans skill.",
    "developer": "Implements plans with TDD in an isolated git worktree and "
                 "addresses review findings.",
    "reviewer": "Independently reviews a developer's branch with the code-review "
                "skill and gates pass/fail. Never reviews its own code.",
}


def prompt_path(profile: str) -> Path:
    return _PROMPT_DIR / f"{profile}.md"


def install_engteam_profiles(*, overwrite: bool = False) -> list[str]:
    created: list[str] = []
    for name in PROFILES:
        if profiles_mod.profile_exists(name) and not overwrite:
            continue
        profiles_mod.create_profile(name, description=PROFILE_DESCRIPTIONS[name])
        created.append(name)
    return created
