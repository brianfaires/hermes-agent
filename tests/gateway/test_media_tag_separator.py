"""Standalone MEDIA directives never join adjacent paths."""
from gateway.platforms.base import MEDIA_TAG_CLEANUP_RE, _strip_media_tag_directives


def test_known_extension_regex_rejects_glued_tags():
    text = "MEDIA:/tmp/file.pngMEDIA:/tmp/file2.png"
    assert list(MEDIA_TAG_CLEANUP_RE.finditer(text)) == []
    assert _strip_media_tag_directives(text) == text


def test_strip_media_directives_handles_separate_known_extension_lines(tmp_path):
    paths = [tmp_path / "a.png", tmp_path / "b.png"]
    for path in paths:
        path.write_bytes(b"PNG")
    text = "\n".join(f"MEDIA:{path}" for path in paths)
    assert [m.group("path") for m in MEDIA_TAG_CLEANUP_RE.finditer(text)] == list(map(str, paths))
    assert _strip_media_tag_directives(text).strip() == ""
