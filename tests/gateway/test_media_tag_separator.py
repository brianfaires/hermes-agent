"""Regression tests for #68773 — MEDIA tags without a separator merge paths.

Before the fix, ``MEDIA_EXTENSIONLESS_TAG_RE`` used a greedy character class
``[^\\s\n`\"']+`` that would silently absorb the next ``MEDIA:`` keyword when
two tags were emitted back-to-back (``MEDIA:/a.pngMEDIA:/b.png``), producing
an invalid merged path that was then rejected by
``validate_media_delivery_path`` and dropped silently.

The same pattern also failed for ``MEDIA:/path/file.pngSome text`` — the
fallback would treat the trailing text as part of the path.
"""

from gateway.platforms.base import (
    _strip_media_tag_directives,
)




def test_glued_control_example_is_not_an_executable_directive(tmp_path):
    """Adjacent tokens must neither merge nor promote a malformed line into delivery."""
    png1 = tmp_path / "a.png"
    png1.write_bytes(b"\x89PNG\r\n\x1a\n")
    png2 = tmp_path / "b.png"
    png2.write_bytes(b"\x89PNG\r\n\x1a\n")

    text = f"MEDIA:{png1}MEDIA:{png2}"
    cleaned = _strip_media_tag_directives(text)
    assert cleaned == text


