"""Tests for front desk turn orchestration planning."""

from gateway.front_desk_orchestrator import plan_front_desk_turn


def test_disabled_front_desk_uses_normal_agent_path():
    result = plan_front_desk_turn(
        "Can you fix the voice thing?",
        config={"agent": {"front_desk": {"enabled": False}}},
    )

    assert result.action == "normal_agent"
    assert result.decision is None
    assert result.immediate_response is None


def test_disabled_routing_uses_normal_agent_path_even_when_frontdesk_enabled():
    result = plan_front_desk_turn(
        "Can you fix the voice thing?",
        config={"agent": {"front_desk": {"enabled": True, "routing": {"enabled": False}}}},
    )

    assert result.action == "normal_agent"


def test_clarify_turn_returns_immediate_response():
    result = plan_front_desk_turn(
        "Can you fix the voice thing?",
        config={"agent": {"front_desk": {"enabled": True}}},
    )

    assert result.action == "respond"
    assert result.decision is not None
    assert result.decision.route == "clarify"
    assert result.immediate_response
    assert result.immediate_response.count("?") == 1


def test_delegate_turn_returns_delegate_plan_without_fake_response():
    result = plan_front_desk_turn(
        "Debug why replies take forever after I stop talking. Check logs.",
        config={"agent": {"front_desk": {"enabled": True}}},
    )

    assert result.action == "delegate"
    assert result.decision is not None
    assert result.decision.team == "engineering"
    assert result.immediate_response is None
