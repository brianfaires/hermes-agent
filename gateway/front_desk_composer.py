"""Front desk response composition helpers."""

from __future__ import annotations

import re

from gateway.front_desk_routing import RouteDecision


_NOISE_PREFIXES = ("traceback", "error details", "error:")
_MAX_WORKER_RESULT_CHARS = 360


def _clean_worker_result(worker_result: str) -> str:
    lines = []
    for raw_line in worker_result.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if any(lowered.startswith(prefix) for prefix in _NOISE_PREFIXES):
            continue
        lines.append(line)

    cleaned = " ".join(lines).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.replace("Root cause:", "Likely cause:")
    cleaned = cleaned.replace("Recommendation:", "Next move:")
    if len(cleaned) > _MAX_WORKER_RESULT_CHARS:
        return cleaned[: _MAX_WORKER_RESULT_CHARS - 1].rstrip() + "…"
    return cleaned


def compose_front_desk_response(
    decision: RouteDecision,
    *,
    worker_result: str | None = None,
    worker_error: str | None = None,
) -> str:
    """Compose a concise Brian-facing response from a route decision.

    This is intentionally deterministic for the first implementation slice. A
    model-based composer can come later, behind tests and evidence boundaries.
    """

    if decision.route == "clarify":
        return decision.clarifying_question or "What do you need clarified?"

    if decision.route == "direct":
        return "I can handle that directly."

    team = decision.team or "the right team"

    if worker_error:
        return f"I couldn’t get {team} back cleanly: {worker_error}."

    if worker_result:
        cleaned = _clean_worker_result(worker_result)
        if cleaned:
            return cleaned
        return f"{team.title()} came back, but there wasn’t a usable result."

    return f"I would hand this to {team}; no handoff has happened yet."
