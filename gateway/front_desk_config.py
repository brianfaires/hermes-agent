"""Front desk profile configuration helpers.

The front desk feature is opt-in. Default Hermes runtime must behave exactly as
before unless ``agent.front_desk.enabled`` is explicitly true.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class FrontDeskConfig:
    enabled: bool = False
    passthrough_delegation: bool = True
    progress_ping_seconds: int = 120
    routing_enabled: bool = True


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return default


def _coerce_positive_int(value: Any, *, default: int) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    return coerced if coerced > 0 else default


def front_desk_config(config: Mapping[str, Any] | None) -> FrontDeskConfig:
    root = _as_mapping(config)
    agent_cfg = _as_mapping(root.get("agent"))
    front_cfg = _as_mapping(agent_cfg.get("front_desk"))
    routing_cfg = _as_mapping(front_cfg.get("routing"))

    return FrontDeskConfig(
        enabled=_coerce_bool(front_cfg.get("enabled"), default=False),
        passthrough_delegation=_coerce_bool(
            front_cfg.get("passthrough_delegation"),
            default=True,
        ),
        progress_ping_seconds=_coerce_positive_int(
            front_cfg.get("progress_ping_seconds"),
            default=120,
        ),
        routing_enabled=_coerce_bool(routing_cfg.get("enabled"), default=True),
    )


def is_front_desk_enabled(config: Mapping[str, Any] | None) -> bool:
    return front_desk_config(config).enabled


def passthrough_delegation_enabled(config: Mapping[str, Any] | None) -> bool:
    return front_desk_config(config).passthrough_delegation


def progress_ping_seconds(config: Mapping[str, Any] | None) -> int:
    return front_desk_config(config).progress_ping_seconds


def routing_enabled(config: Mapping[str, Any] | None) -> bool:
    return front_desk_config(config).routing_enabled
