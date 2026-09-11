"""Context-local output generation for the existing Discord session task tree."""
from contextvars import ContextVar

# Child asyncio tasks and gateway copy_context executor calls inherit this.
# The adapter identity prevents a token from being used by another profile.
output_scope = ContextVar("discord_voice_output_scope", default=None)


def voice_control_transcripts(transcript):
    """Exact spoken controls, after configured aliases; unknown speech is untouched.

    An explicit correction uses normal /stop then a fresh user turn, so old
    partial speech is never resumed as if it were the corrected answer.
    """
    import re
    if transcript.startswith('/'):
        return (transcript,)
    normalized = re.sub(r'[^\w\s]', '', transcript.casefold())
    normalized = ' '.join(normalized.split())
    if normalized in {'status', 'status please', 'what is the status', 'are you still working'}:
        return ('/status',)
    if normalized in {'stop', 'stop talking', 'stop working', 'cancel'}:
        return ('/stop',)
    match = re.fullmatch(r'correction\s*[:,]?\s+(.+)', transcript, flags=re.IGNORECASE | re.DOTALL)
    if match and match.group(1).strip():
        return ('/stop', match.group(1).strip())
    return (transcript,)
