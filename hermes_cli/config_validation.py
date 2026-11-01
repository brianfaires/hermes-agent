"""Read-only validation for skill budgets and quoted config containers."""
from __future__ import annotations

import shlex
from typing import Any, TYPE_CHECKING

import hermes_yaml as yaml

if TYPE_CHECKING:
    from hermes_cli.config import ConfigIssue


def _validate_quoted_containers(config: dict[str, Any], issues: list[ConfigIssue]) -> None:
    """A container slot holding ONE quoted string (``enabled: '["a","b"]'``) is skipped by every
    isinstance-gated reader while ``config get`` echoes it back, so plugins silently unmount and
    exclusions silently lapse (#83308, #105706). Finding only — the file is never rewritten."""
    from hermes_cli.config import _container_slots, _SCALAR_AS_ONE_ITEM_LIST_KEYS, cfg_get, _looks_structured_value, _issue

    for key, kind in _container_slots().items():
        # ``parse_config_string_list`` readers accept the quoted form; nothing is ignored there.
        if key in _SCALAR_AS_ONE_ITEM_LIST_KEYS:
            continue
        value = cfg_get(config, *key.split("."))
        if not isinstance(value, str) or not _looks_structured_value(value):
            continue
        try:
            parsed = yaml.safe_load(value)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, (list, dict)):
            _issue(issues, "warning",
                   f"{key} is the quoted string {value!r} — Hermes expects a YAML {kind} here "
                   "and every reader ignores the string",
                   f"Run: hermes config set {key} {shlex.quote(value)}  (stores a real {kind}), "
                   "or remove the quotes in config.yaml")


def _validate_skill_description_limit(config: dict[str, Any], issues: list[ConfigIssue]) -> None:
    """``skills.prompt_description_limit`` must be an int >= 20 or ``full``; otherwise the index uses 60."""
    from hermes_cli.config import _issue

    skills = config.get("skills")
    if not isinstance(skills, dict) or "prompt_description_limit" not in skills:
        return
    from agent.skill_utils import coerce_prompt_description_limit
    try:
        coerce_prompt_description_limit(skills["prompt_description_limit"])
    except ValueError as exc:
        _issue(issues, "error", str(exc),
               "Set an integer such as 120, or 'full' to show whole descriptions; until fixed the skill index uses 60")
