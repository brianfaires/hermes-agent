"""Front desk turn planning.

This module provides a small, deterministic seam that gateway wiring can call
without embedding front desk policy directly in ``gateway.run``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from gateway.front_desk_composer import compose_front_desk_response
from gateway.front_desk_config import is_front_desk_enabled, routing_enabled
from gateway.front_desk_routing import RouteDecision, classify_front_desk_route


@dataclass(frozen=True)
class FrontDeskTurnPlan:
    action: str
    decision: RouteDecision | None = None
    immediate_response: str | None = None


def plan_front_desk_turn(message: str, *, config: Mapping[str, Any] | None) -> FrontDeskTurnPlan:
    """Plan how a front desk turn should proceed.

    Actions:
    - ``normal_agent``: preserve existing gateway behavior.
    - ``respond``: return ``immediate_response`` directly.
    - ``delegate``: caller should run the appropriate worker path.
    """

    if not is_front_desk_enabled(config) or not routing_enabled(config):
        return FrontDeskTurnPlan(action="normal_agent")

    decision = classify_front_desk_route(message)

    if decision.route == "clarify":
        return FrontDeskTurnPlan(
            action="respond",
            decision=decision,
            immediate_response=compose_front_desk_response(decision),
        )

    if decision.route == "delegate":
        if decision.team == "engineering":
            return FrontDeskTurnPlan(action="handoff", decision=decision)
        return FrontDeskTurnPlan(action="delegate", decision=decision)

    return FrontDeskTurnPlan(action="normal_agent", decision=decision)
