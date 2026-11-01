"""Pin the skill-index budget to a conversation, including resumed/compacted sessions."""
from pathlib import Path
from typing import Any

from agent.skill_utils import SKILL_PROMPT_DESC_LIMIT, coerce_prompt_description_limit, skill_prompt_description_limit
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

_BUDGET_KEY = "skills_prompt_description_limit"


def session_description_limit(agent: Any, home: Path | None) -> int | None:
    """Resolve once at initialization; prompt rebuilds perform no database operations."""
    if "_skills_prompt_description_limit" in vars(agent):
        return agent._skills_prompt_description_limit
    config = getattr(agent, "_session_init_model_config", None)
    if not isinstance(config, dict):
        config = agent._session_init_model_config = {}
    db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    read = getattr(db, "get_session_model_config_value", None)
    stored = config.get(_BUDGET_KEY)
    if stored is None and callable(read) and session_id:
        stored = read(session_id, _BUDGET_KEY)
    if stored is None:
        token = set_hermes_home_override(home) if home is not None else None
        try:
            limit = skill_prompt_description_limit()
        finally:
            if token is not None:
                reset_hermes_home_override(token)
        # Stored pre-feature prompts have the implicit default; keep it implicit
        # so ordinary compression never rewrites unrelated model_config state.
        if callable(read) and session_id and (db.get_session(session_id) or {}).get("system_prompt"):
            limit = SKILL_PROMPT_DESC_LIMIT
        stored = "full" if limit is None else limit
        if limit != SKILL_PROMPT_DESC_LIMIT and callable(read) and session_id:
            written = db.update_conversation_model_config_value(
                session_id, _BUDGET_KEY, lambda current: stored if current is None else current,
            )
            if written:
                stored = read(written, _BUDGET_KEY)
    resolved = coerce_prompt_description_limit(stored)
    if resolved != SKILL_PROMPT_DESC_LIMIT:
        config[_BUDGET_KEY] = stored
    agent._skills_prompt_description_limit = resolved
    return resolved
