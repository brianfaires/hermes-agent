"""Deterministic front desk routing helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RouteDecision:
    route: str
    team: str | None
    reason: str
    clarifying_question: str | None = None


_ENGINEERING_TERMS = (
    "debug",
    "log",
    "logs",
    "traceback",
    "error",
    "bug",
    "clipped",
    "latency",
    "endpoint",
    "code",
    "test",
)
_OPS_TERMS = (
    "service",
    "gateway",
    "cron",
    "systemctl",
    "running",
    "restart",
    "status",
    "process",
)
_RESEARCH_TERMS = (
    "research",
    "latest",
    "docs",
    "documentation",
    "web",
    "find out",
    "look up",
)
_PLANNING_TERMS = (
    "plan",
    "roadmap",
    "breakdown",
    "rollout",
    "milestones",
)
_GREETING_TERMS = (
    "hey",
    "hello",
    "hi",
    "alive",
    "you there",
)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def classify_front_desk_route(message: str) -> RouteDecision:
    text = (message or "").strip().lower()

    if not text:
        return RouteDecision(
            route="clarify",
            team=None,
            reason="empty or missing request",
            clarifying_question="What do you need?",
        )

    if "voice thing" in text or ("fix" in text and "voice" in text and len(text.split()) <= 8):
        return RouteDecision(
            route="clarify",
            team=None,
            reason="ambiguous voice issue",
            clarifying_question="Which voice issue do you mean: hearing you, reply audio, or cutoff timing?",
        )

    if _contains_any(text, _RESEARCH_TERMS):
        return RouteDecision(
            route="delegate",
            team="research",
            reason="research or current-documentation request",
        )

    if _contains_any(text, _PLANNING_TERMS):
        return RouteDecision(
            route="delegate",
            team="planning",
            reason="planning request",
        )

    if _contains_any(text, _OPS_TERMS) and not _contains_any(text, _ENGINEERING_TERMS):
        return RouteDecision(
            route="delegate",
            team="ops",
            reason="runtime or service operation request",
        )

    if _contains_any(text, _ENGINEERING_TERMS):
        return RouteDecision(
            route="delegate",
            team="engineering",
            reason="debug/log/code request",
        )

    if _contains_any(text, _GREETING_TERMS):
        return RouteDecision(
            route="direct",
            team=None,
            reason="lightweight conversational turn",
        )

    return RouteDecision(
        route="direct",
        team=None,
        reason="lightweight conversational turn",
    )
