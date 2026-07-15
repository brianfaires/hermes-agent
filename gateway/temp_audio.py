"""Profile-scoped temporary audio paths for gateway-generated TTS."""

from __future__ import annotations

import hashlib
import re
import tempfile
import uuid
from pathlib import Path

from hermes_constants import get_hermes_home

_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _profile_temp_segment() -> str:
    """Return a stable, filesystem-safe segment for the active Hermes profile."""
    home = get_hermes_home()
    if home.parent.name == "profiles" and home.name:
        raw = home.name
    else:
        try:
            default_home = Path.home() / ".hermes"
            raw = "default" if home.resolve() == default_home.resolve() else ""
        except OSError:
            raw = ""
        if not raw:
            digest = hashlib.sha256(str(home).encode("utf-8", "surrogatepass")).hexdigest()[:12]
            raw = f"home-{digest}"
    segment = _SAFE_SEGMENT_RE.sub("_", raw).strip("._-")
    if segment:
        return segment
    digest = hashlib.sha256(str(home).encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"home-{digest}"


def gateway_tts_temp_path(prefix: str, extension: str) -> str:
    """Return a unique profile-scoped temp path for gateway-generated TTS audio."""
    safe_prefix = _SAFE_SEGMENT_RE.sub("_", prefix).strip("._-") or "tts"
    safe_ext = _SAFE_SEGMENT_RE.sub("", extension.lstrip(".")) or "mp3"
    temp_dir = Path(tempfile.gettempdir()) / "hermes_voice" / _profile_temp_segment()
    temp_dir.mkdir(parents=True, exist_ok=True)
    return str(temp_dir / f"{safe_prefix}_{uuid.uuid4().hex[:12]}.{safe_ext}")
