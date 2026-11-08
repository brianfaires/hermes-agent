"""Carry original command text until the gateway resolves the destination profile."""
import re


def original_command_text(original: str, bot_id) -> str | None:
    text = original.lstrip()
    if bot_id is not None:
        # Remove addressing at the start; mentions inside a journal note are content.
        text = re.sub(rf"^(?:<@!?{re.escape(str(bot_id))}>\s*)+", "", text)
    return text if text.startswith("/") else None


def stash_original_command_text(event, original: str, bot_id) -> None:
    """Record the command as typed on *event* for text-mode plugin commands (see ``MessageEvent.verbatim_command_text``)."""
    if command := original_command_text(original, bot_id):
        event.metadata["original_command_text"] = command
