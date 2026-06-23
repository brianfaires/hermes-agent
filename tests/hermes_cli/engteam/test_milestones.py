# tests/hermes_cli/engteam/test_milestones.py
import pytest
from hermes_cli.engteam.milestones import format_milestone, MILESTONE_KINDS


def test_stage_done_line():
    line = format_milestone(kind="stage_done", goal="dark mode", stage="spec")
    assert "dark mode" in line and "spec" in line.lower()
    assert "\n" not in line


def test_gate_requested_mentions_user_action():
    line = format_milestone(kind="gate_requested", goal="dark mode",
                            stage="commit", detail="merge approval")
    assert "merge approval" in line


def test_complete_and_failed_distinct():
    done = format_milestone(kind="complete", goal="x")
    failed = format_milestone(kind="failed", goal="x", detail="ran out of rounds")
    assert done != failed and "ran out of rounds" in failed


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        format_milestone(kind="bogus", goal="x")


def test_kinds_are_frozen_set():
    assert "escalation" in MILESTONE_KINDS
