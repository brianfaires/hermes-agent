"""Tests for deterministic front desk routing."""

from gateway.front_desk_routing import RouteDecision, classify_front_desk_route


def test_greeting_routes_direct():
    decision = classify_front_desk_route("Hey Extra, are you alive?")

    assert decision == RouteDecision(
        route="direct",
        team=None,
        reason="lightweight conversational turn",
        clarifying_question=None,
    )


def test_ambiguous_voice_issue_asks_one_question():
    decision = classify_front_desk_route("Can you fix the voice thing?")

    assert decision.route == "clarify"
    assert decision.team is None
    assert decision.clarifying_question
    assert decision.clarifying_question.count("?") == 1
    assert "voice" in decision.clarifying_question.lower()


def test_debug_logs_route_to_engineering():
    decision = classify_front_desk_route(
        "Debug why my voice message clipped after the first phrase. Check the logs."
    )

    assert decision.route == "delegate"
    assert decision.team == "engineering"
    assert "debug" in decision.reason.lower() or "log" in decision.reason.lower()


def test_service_status_routes_to_ops():
    decision = classify_front_desk_route("Check whether the gateway service is running.")

    assert decision.route == "delegate"
    assert decision.team == "ops"


def test_current_docs_route_to_research():
    decision = classify_front_desk_route("Research the latest Discord voice docs and summarize them.")

    assert decision.route == "delegate"
    assert decision.team == "research"


def test_make_a_plan_routes_to_planning():
    decision = classify_front_desk_route("Make a plan for the engineering team rollout.")

    assert decision.route == "delegate"
    assert decision.team == "planning"
