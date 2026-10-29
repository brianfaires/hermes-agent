"""Pin the skill-index budget to a conversation, including resumed/compacted sessions."""
from pathlib import Path
from typing import Any

from agent.skill_utils import SKILL_PROMPT_DESC_LIMIT, coerce_prompt_description_limit, skill_prompt_description_limit
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

_BUDGET_KEY = "skills_prompt_description_limit"


def session_description_limit(agent: Any, home: Path | None) -> int | None:
    """Use native session model_config persistence rather than rereading config on compression."""
    config = getattr(agent, "_session_init_model_config", None)
    if not isinstance(config, dict):
        config = agent._session_init_model_config = {}
    if _BUDGET_KEY in config:
        return coerce_prompt_description_limit(config[_BUDGET_KEY])

    db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    stored = db.get_session_model_config_value(session_id, _BUDGET_KEY) if db and session_id else None
    if stored is None:
        token = set_hermes_home_override(home) if home is not None else None
        try:
            limit = skill_prompt_description_limit()
        finally:
            if token is not None:
                reset_hermes_home_override(token)
        # Pre-feature sessions with a stored prompt used the old 60-character budget.
        if db and session_id and (db.get_session(session_id) or {}).get("system_prompt"):
            limit = SKILL_PROMPT_DESC_LIMIT
        stored = "full" if limit is None else limit
        if db and session_id:
            written = db.update_conversation_model_config_value(
                session_id, _BUDGET_KEY, lambda current: stored if current is None else current,
            )
            if written:
                stored = db.get_session_model_config_value(written, _BUDGET_KEY)
    config[_BUDGET_KEY] = stored
    return coerce_prompt_description_limit(stored)
