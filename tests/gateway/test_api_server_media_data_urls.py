"""MEDIA: tag → base64 data-URL resolution for the API server (salvage of #2696).

Remote OpenAI-compatible frontends can't read local file paths, so
``MEDIA:<path>`` image tags in final responses are inlined as markdown
data URLs before crossing the HTTP boundary.
"""

import base64
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("aiohttp")

from gateway.platforms.api_server import _resolve_media_to_data_urls  # noqa: E402

# 1x1 transparent PNG
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    "h6FO1AAAAABJRU5ErkJggg=="
)


class TestResolveMediaToDataUrls(unittest.TestCase):
    def _write_png(self, tmpdir_name="hermes_media_test"):
        import tempfile
        from pathlib import Path

        directory = tempfile.TemporaryDirectory(prefix=tmpdir_name)
        self.addCleanup(directory.cleanup)
        d = Path(directory.name)
        p = d / "shot.png"
        p.write_bytes(_PNG_BYTES)
        return p

    def test_media_tag_inlined(self):
        p = self._write_png()
        out = _resolve_media_to_data_urls(f"Here you go:\nMEDIA:{p}")
        self.assertIn("data:image/png;base64,", out)
        self.assertNotIn("MEDIA:", out)
        self.assertEqual(out, "Here you go:\n![image](data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode() + ")")

    def test_backtick_wrapped_tag(self):
        p = self._write_png()
        out = _resolve_media_to_data_urls(f"See `MEDIA:{p}` above")
        text = f"See `MEDIA:{p}` above"
        self.assertEqual(out, text)
        # Existing files in examples must never be read or embedded.
        with patch.object(Path, "read_bytes", side_effect=AssertionError("literal example read")):
            self.assertEqual(_resolve_media_to_data_urls(text), text)

    def test_prose_tag_remains_literal_with_existing_file(self):
        p = self._write_png()
        text = f"Here you go: MEDIA:{p}"
        with patch.object(Path, "read_bytes", side_effect=AssertionError("literal example read")):
            self.assertEqual(_resolve_media_to_data_urls(text), text)

    def test_missing_file_left_untouched(self):
        text = "MEDIA:/nonexistent/path/shot.png"
        self.assertEqual(_resolve_media_to_data_urls(text), text)

    def test_non_image_left_untouched(self):
        text = "MEDIA:/tmp/archive.zip"
        self.assertEqual(_resolve_media_to_data_urls(text), text)


if __name__ == "__main__":
    unittest.main()
