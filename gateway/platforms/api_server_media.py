"""Render validated standalone image controls for remote API frontends."""
import base64
from pathlib import Path

from gateway import media_directives

_MIME = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
         '.webp': 'image/webp', '.bmp': 'image/bmp'}
_MAX_BYTES = 5 * 1024 * 1024


def _data_url(path):
    from gateway.platforms import base

    safe = base.validate_media_delivery_path(path)
    if not safe:
        return None
    file = Path(safe)
    suffix = file.suffix.lower()
    if suffix not in _MIME:
        return None
    try:
        if file.stat().st_size > _MAX_BYTES:
            return None
        encoded = base64.b64encode(file.read_bytes()).decode()
    except OSError:
        return None
    return f'![image](data:{_MIME[suffix]};base64,{encoded})'


def resolve_media_to_data_urls(text):
    from gateway.platforms import base

    if not text or 'media:' not in text.lower():
        return text
    try:
        sentinel = base._terminal_sentinel_start(text)
        original = text[:sentinel] if sentinel >= 0 else text
        replacements = [(start, end, rendered)
                        for start, end, path in media_directives.tag_matches(media_directives.masked_scan(original))
                        if (rendered := _data_url(path)) is not None]
        if not replacements:
            return text
        for start, end, rendered in reversed(replacements):
            original = original[:start] + rendered + original[end:]
        return original
    except Exception:
        # Invalid paths/config must leave a readable response, never break the API turn.
        return text
