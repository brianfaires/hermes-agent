"""Bridge a front-desk engineering delegation into an engteam project."""
from __future__ import annotations

from typing import Callable

from hermes_cli.engteam import registry


def handoff_engineering(message: str, *, opener: Callable = registry.open_project) -> str:
    goal = (message or "").strip()
    opener(goal=goal)
    return ("Engineering picked it up — I've opened a project and the lead is "
            "scoping it now. I'll report milestones as they land.")
