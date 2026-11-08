"""Carry original command text until the gateway resolves the destination profile."""
import re


def original_command_text(original: str, bot_id) -> str | None:
    text = original.lstrip()
    if bot_id is not None:
        # Remove addressing at the start; mentions inside a journal note are content.
        text = re.sub(rf"^(?:<@!?{re.escape(str(bot_id))}>\s*)+", "", text)
    return text if text.startswith("/") else None
