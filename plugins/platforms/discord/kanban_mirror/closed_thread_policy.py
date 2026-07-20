"""Config-driven routing for replies targeting closed Discord mirror threads."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VALID_THREAD_STATES = {"active", "archived", "locked", "missing"}
VALID_ACTIONS = {"discard", "redirect", "reopen_thread"}


@dataclass(frozen=True)
class ClosedThreadRule:
    match: dict[str, str] = field(default_factory=dict)
    action: str = "discard"
    destination: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ClosedThreadPolicy:
    default_action: str = "discard"
    states: dict[str, str] = field(default_factory=lambda: {"archived": "discard", "locked": "discard", "missing": "discard"})
    rules: tuple[ClosedThreadRule, ...] = ()
    failure_policy: dict[str, str] = field(default_factory=lambda: {"redirect_failure": "log_only", "reopen_failure": "log_and_kanban_comment"})


def _as_str_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if v is not None}


def _action(value: Any, fallback: str = "discard") -> str:
    candidate = str(value or fallback).strip()
    return candidate if candidate in VALID_ACTIONS else fallback


def load_closed_thread_policy(raw: Any) -> ClosedThreadPolicy:
    if not isinstance(raw, dict):
        raw = {}
    state_defaults = {k: _action(v) for k, v in _as_str_dict(raw.get("states")).items() if k in VALID_THREAD_STATES - {"active"}}
    if not state_defaults:
        state_defaults = {"archived": "discard", "locked": "discard", "missing": "discard"}
    rules: list[ClosedThreadRule] = []
    for raw_rule in raw.get("rules") or []:
        if not isinstance(raw_rule, dict):
            continue
        rules.append(
            ClosedThreadRule(
                match=_as_str_dict(raw_rule.get("match")),
                action=_action(raw_rule.get("action")),
                destination=_as_str_dict(raw_rule.get("destination")),
            )
        )
    return ClosedThreadPolicy(
        default_action=_action(raw.get("default_action")),
        states=state_defaults,
        rules=tuple(rules),
        failure_policy=_as_str_dict(raw.get("failure_policy")) or {"redirect_failure": "log_only", "reopen_failure": "log_and_kanban_comment"},
    )


def resolve_closed_thread_action(policy: ClosedThreadPolicy, context: dict[str, Any]) -> tuple[str, dict[str, str] | None]:
    ctx = {str(k): str(v) for k, v in context.items() if v is not None}
    for rule in policy.rules:
        if all(ctx.get(k) == str(v) for k, v in rule.match.items()):
            return rule.action, rule.destination or None
    state = ctx.get("thread_state", "")
    return policy.states.get(state, policy.default_action), None


def classify_thread_state(channel: dict[str, Any] | None) -> str:
    if not channel:
        return "missing"
    meta = channel.get("thread_metadata") or {}
    # Discord Forum threads are commonly locked while archived. Treat locked as
    # the more restrictive state so locked-specific policy rules are reachable.
    if bool(meta.get("locked")):
        return "locked"
    if bool(meta.get("archived")):
        return "archived"
    return "active"
