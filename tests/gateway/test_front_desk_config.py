"""Tests for front desk profile configuration helpers."""

from gateway.front_desk_config import (
    front_desk_config,
    is_front_desk_enabled,
    passthrough_delegation_enabled,
    progress_ping_seconds,
    routing_enabled,
)


def test_front_desk_disabled_by_default():
    config = {}

    assert is_front_desk_enabled(config) is False
    assert passthrough_delegation_enabled(config) is True
    assert routing_enabled(config) is True
    assert progress_ping_seconds(config) == 120


def test_default_config_contains_front_desk_defaults():
    from hermes_cli.config import DEFAULT_CONFIG

    front_desk = DEFAULT_CONFIG["agent"]["front_desk"]

    assert front_desk["enabled"] is False
    assert front_desk["passthrough_delegation"] is True
    assert front_desk["progress_ping_seconds"] == 120
    assert front_desk["routing"]["enabled"] is True


def test_front_desk_reads_nested_agent_config():
    config = {
        "agent": {
            "front_desk": {
                "enabled": True,
                "passthrough_delegation": False,
                "progress_ping_seconds": 30,
                "routing": {"enabled": False},
            }
        }
    }

    assert is_front_desk_enabled(config) is True
    assert passthrough_delegation_enabled(config) is False
    assert routing_enabled(config) is False
    assert progress_ping_seconds(config) == 30


def test_front_desk_coerces_common_scalar_values():
    config = {
        "agent": {
            "front_desk": {
                "enabled": "true",
                "passthrough_delegation": "no",
                "progress_ping_seconds": "90",
                "routing": {"enabled": "yes"},
            }
        }
    }

    assert front_desk_config(config).enabled is True
    assert passthrough_delegation_enabled(config) is False
    assert routing_enabled(config) is True
    assert progress_ping_seconds(config) == 90


def test_front_desk_malformed_values_fail_closed():
    config = {
        "agent": {
            "front_desk": {
                "enabled": "banana",
                "passthrough_delegation": "banana",
                "progress_ping_seconds": -5,
                "routing": "banana",
            }
        }
    }

    assert is_front_desk_enabled(config) is False
    assert passthrough_delegation_enabled(config) is True
    assert routing_enabled(config) is True
    assert progress_ping_seconds(config) == 120
