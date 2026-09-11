"""Context-local output generation for the existing Discord session task tree."""
from contextvars import ContextVar

# Child asyncio tasks and gateway copy_context executor calls inherit this.
# The adapter identity prevents a token from being used by another profile.
output_scope = ContextVar("discord_voice_output_scope", default=None)
