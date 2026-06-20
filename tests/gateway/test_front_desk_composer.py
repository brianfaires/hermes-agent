"""Tests for front desk response composition."""

from gateway.front_desk_composer import compose_front_desk_response
from gateway.front_desk_routing import RouteDecision


def test_clarify_route_returns_single_question():
    decision = RouteDecision(
        route="clarify",
        team=None,
        reason="ambiguous voice issue",
        clarifying_question="Which voice issue do you mean: hearing you, reply audio, or cutoff timing?",
    )

    response = compose_front_desk_response(decision)

    assert response == "Which voice issue do you mean: hearing you, reply audio, or cutoff timing?"
    assert response.count("?") == 1


def test_worker_success_is_summarized_without_raw_log_dump():
    decision = RouteDecision(
        route="delegate",
        team="engineering",
        reason="debug/log request",
        clarifying_question=None,
    )
    worker_result = (
        "TRACEBACK line 1\n"
        "ERROR details\n"
        "ERROR: token refresh failed\n"
        "Root cause: endpointing fired after 1 second. Recommendation: set silence to 5 seconds."
    )

    response = compose_front_desk_response(decision, worker_result=worker_result)

    assert "endpointing" in response.lower()
    assert "5 seconds" in response
    assert "TRACEBACK" not in response
    assert "ERROR" not in response


def test_worker_success_truncates_long_results():
    decision = RouteDecision(
        route="delegate",
        team="engineering",
        reason="debug/log request",
        clarifying_question=None,
    )

    response = compose_front_desk_response(decision, worker_result="x" * 1000)

    assert len(response) < 500
    assert response.endswith("…")


def test_worker_failure_is_honest():
    decision = RouteDecision(
        route="delegate",
        team="engineering",
        reason="debug/log request",
        clarifying_question=None,
    )

    response = compose_front_desk_response(decision, worker_error="worker timed out")

    assert "couldn’t get engineering back" in response.lower()
    assert "worker timed out" in response


def test_delegate_without_result_does_not_fake_handoff():
    decision = RouteDecision(
        route="delegate",
        team="engineering",
        reason="debug/log request",
        clarifying_question=None,
    )

    response = compose_front_desk_response(decision)

    assert "would hand this to engineering" in response.lower()
    assert "sent" not in response.lower()
    assert "checked" not in response.lower()
