"""Turn engteam events into one concise Brian-facing line for the front desk.

Kept deterministic and dependency-free so the gateway notifier can call it
without importing the kanban DB layer."""
from __future__ import annotations

MILESTONE_KINDS = frozenset(
    {"stage_done", "gate_requested", "escalation", "blocked", "complete", "failed"}
)


def format_milestone(*, kind: str, goal: str, stage: str | None = None,
                     detail: str = "") -> str:
    if kind not in MILESTONE_KINDS:
        raise ValueError(f"unknown milestone kind: {kind!r}")
    tail = f" — {detail}" if detail else ""
    if kind == "stage_done":
        return f"{goal}: {stage} stage done{tail}."
    if kind == "gate_requested":
        return f"{goal}: waiting on you to approve {detail or stage}."
    if kind == "escalation":
        return f"{goal}: the lead needs a decision{tail}."
    if kind == "blocked":
        return f"{goal}: blocked{tail}."
    if kind == "complete":
        return f"{goal}: shipped{tail}."
    return f"{goal}: stalled{tail}."
