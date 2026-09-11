"""MEDIA cleanup accepts standalone directives, never prose examples."""
from gateway.platforms.base import BasePlatformAdapter, MEDIA_TAG_CLEANUP_RE, _strip_media_tag_directives

class TestMediaTagCleanup:
    def test_standalone_document_marker_still_delivers(self):
        text = "MEDIA:/tmp/report.xlsx[[as_document]]"
        media, cleaned = BasePlatformAdapter.extract_media(text)
        assert media == [("/tmp/report.xlsx", False)]
        assert cleaned == ""
        assert _strip_media_tag_directives(text) == ""

    def test_inline_document_marker_does_not_turn_prose_into_delivery(self):
        text = "Example MEDIA:/tmp/report.xlsx[[as_document]]"
        media, cleaned = BasePlatformAdapter.extract_media(text)
        assert media == []
        assert cleaned == "Example MEDIA:/tmp/report.xlsx"

class TestMediaTagCjkTerminators:
    def test_caption_suffixes_are_prose_not_standalone_directives(self):
        for suffix in ("（782.6 KB）", "：内容", "，下一条", " 782 KB", ", next"):
            text = "MEDIA:/tmp/report.pdf" + suffix
            assert MEDIA_TAG_CLEANUP_RE.search(text) is None
            assert _strip_media_tag_directives(text) == text

    def test_chinese_filename_on_own_line_delivers(self):
        text = "## 交付物\nMEDIA:/tmp/早报.pdf"
        media, cleaned = BasePlatformAdapter.extract_media(text)
        assert media == [("/tmp/早报.pdf", False)]
        assert cleaned == "## 交付物"
