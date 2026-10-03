"""Attachment delivery syntax is not a spoken script; no provider calls."""
import pytest

from gateway.platforms.base import BasePlatformAdapter
from tools.tts_text_normalize import prepare_spoken_text
from tools.tts_text_normalize import strip_attachment_references
from tools.tts_tool import _strip_markdown_for_tts
from tools.tts_streaming import SentenceChunker


@pytest.mark.parametrize("cleaner", [prepare_spoken_text, _strip_markdown_for_tts,
    lambda text: BasePlatformAdapter.prepare_tts_text(None, text)])
def test_attachment_directive_is_not_spoken(cleaner):
    raw = "Here is the report.\n[[as_document]]\nMEDIA:/tmp/quarterly-report.pdf"
    assert cleaner(raw) == "Here is the report."


@pytest.mark.parametrize("reference", [
    "[quarterly-report.pdf](/tmp/quarterly-report.pdf)",
    "![quarterly-report.pdf](/tmp/quarterly-report.pdf)",
    "[`quarterly-report.pdf`](/tmp/quarterly-report.pdf)",
    "[r\u00e9sum\u00e9 final.pdf](/tmp/r\u00e9sum\u00e9 final.pdf)",
    "[r\u00e9sum\u00e9 final.pdf](file:///tmp/r%C3%A9sum%C3%A9%20final.pdf)",
])
def test_local_attachment_filename_label_is_not_spoken(reference):
    assert prepare_spoken_text("Here is the report.\n" + reference) == "Here is the report."


def test_streamed_attachment_path_is_not_split_into_spoken_fragments():
    raw = "MEDIA:/tmp/quarterly. report r\u00e9sum\u00e9.pdf\n\nAll done."
    chunker = SentenceChunker()
    spoken = []
    for char in raw:
        spoken.extend(_strip_markdown_for_tts(part) for part in chunker.feed(char))
    spoken.extend(_strip_markdown_for_tts(part) for part in chunker.flush())
    assert " ".join(part for part in spoken if part) == "All done."


def test_legacy_fallback_does_not_speak_attachment_metadata(monkeypatch):
    import tools.tts_text_normalize as normalizer

    def fail(*args, **kwargs):
        raise ValueError("forced normalization failure")

    monkeypatch.setattr(normalizer, "prepare_spoken_text", fail)
    raw = "Done.\nMEDIA:/tmp/report.pdf"
    assert _strip_markdown_for_tts(raw) == "Done."
    assert BasePlatformAdapter.prepare_tts_text(None, raw) == "Done."


@pytest.mark.parametrize("directive", [
    "MEDIA:/tmp/report.pdf", 'MEDIA: "/tmp/r\u00e9sum\u00e9 final.pdf"',
    "**MEDIA:/tmp/report.pdf**", "- MEDIA:/tmp/report final.pdf",
    "MEDIA:~/report.pdf", r"MEDIA:C:\reports\final report.pdf",
    "[[as_document]]/tmp/report.pdf", "[[audio_as_voice]]/tmp/voice.ogg",
    "[[audio_as_voice]]\nMEDIA:/tmp/voice.ogg",
    "[[as_document]]MEDIA:/tmp/report.pdf",
    "[[audio_as_voice]] MEDIA:/tmp/voice.ogg",
    "MEDIA:/tmp/report.pdf\nMEDIA:/tmp/chart.png",
])
def test_pure_attachment_response_has_no_speech(directive):
    assert prepare_spoken_text(directive) == ""


@pytest.mark.parametrize("raw", [
    "Edit config.yaml and /tmp/notes.txt before running.",
    "The filename quarterly-report.pdf matters.",
    "The example is MEDIA:/tmp/report.pdf, not an attachment.",
    "[API documentation](https://example.com/api)",
    "[report.pdf](https://example.com/report.pdf)",
    "[Read the report](/tmp/report.pdf)",
])
def test_ordinary_file_discussion_and_descriptive_labels_are_preserved(raw):
    assert strip_attachment_references(raw) == raw


def test_original_attachment_delivery_is_unchanged():
    raw = "Done.\n[[as_document]]\nMEDIA:/tmp/r\u00e9sum\u00e9 final.pdf\nMEDIA:/tmp/chart.png"
    before = BasePlatformAdapter.extract_media(raw)
    assert prepare_spoken_text(raw) == "Done."
    assert BasePlatformAdapter.extract_media(raw) == before
    assert before[0] == [("/tmp/r\u00e9sum\u00e9 final.pdf", False), ("/tmp/chart.png", False)]


@pytest.mark.parametrize("normalizer_failure", [False, True])
@pytest.mark.parametrize("raw", [
    "MEDIA:/tmp/report.pdf",
    "[[as_document]]MEDIA:/tmp/report.pdf",
    "[[audio_as_voice]] MEDIA:/tmp/voice.ogg",
])
def test_explicit_tool_rejects_attachment_only_text_before_provider_call(monkeypatch, normalizer_failure, raw):
    import tools.tts_tool as tts
    import tools.tts_text_normalize as normalizer
    import json

    def unexpected(*args, **kwargs):
        raise AssertionError("attachment-only text must not reach provider resolution")

    if normalizer_failure:
        def fail(*args, **kwargs):
            raise ValueError("forced normalization failure")
        monkeypatch.setattr(normalizer, "prepare_spoken_text", fail)
    monkeypatch.setattr(tts, "_load_tts_config", unexpected)
    result = json.loads(tts.text_to_speech_tool(text=raw))
    assert result["success"] is False
    assert result["error"] == "Text is empty after TTS cleanup"


def test_streaming_inline_media_example_preserves_prose():
    chunker = SentenceChunker()
    parts = chunker.feed("This is an example only. ")
    parts += chunker.feed("MEDIA:/tmp/report.pdf, not an attachment.")
    parts += chunker.flush()
    assert " ".join(_strip_markdown_for_tts(part) for part in parts) == (
        "This is an example only. MEDIA:/tmp/report. pdf, not an attachment."
    )


def test_idle_flush_preserves_unfinished_attachment_context():
    chunker = SentenceChunker()
    parts = chunker.feed("MEDIA:/tmp/quarterly. ")
    parts += chunker.flush(final=False)
    parts += chunker.feed("report.pdf\n\nAll done.")
    parts += chunker.flush()
    assert " ".join(part for part in map(_strip_markdown_for_tts, parts) if part) == "All done."


def test_idle_flush_still_delivers_ordinary_prose_promptly():
    chunker = SentenceChunker()
    assert chunker.feed("Let me check.") == []
    assert chunker.flush(final=False) == ["Let me check."]
    assert chunker.buf == ""


@pytest.mark.parametrize("idle", [False, True])
@pytest.mark.parametrize("characterwise", [False, True])
def test_streamed_inline_reference_keeps_original_line_context(idle, characterwise):
    chunker = SentenceChunker()
    parts = chunker.feed("This is an example only. " if not idle else "This is an example only.")
    if idle:
        parts += chunker.flush(final=False)
    suffix = " MEDIA:/tmp/report.pdf" if idle else "MEDIA:/tmp/report.pdf"
    for delta in suffix if characterwise else [suffix]:
        parts += chunker.feed(delta)
    parts += chunker.flush()
    spoken = [_strip_markdown_for_tts(part) for part in parts]
    assert " ".join(spoken) == "This is an example only. MEDIA:/tmp/report. pdf"
    # The sync fallback calls the explicit tool, which normalizes again.
    assert " ".join(prepare_spoken_text(part) for part in spoken) == " ".join(spoken)


@pytest.mark.parametrize("path", ["/tmp/Makefile", "/tmp/report,final.pdf", "/tmp/report;final.pdf"])
def test_unusual_attachment_paths_are_not_spoken(path):
    assert prepare_spoken_text("Done.\nMEDIA:" + path) == "Done."


@pytest.mark.parametrize("reference", [
    "[quarterly. report.pdf](/tmp/quarterly. report.pdf)",
    "![r\u00e9sum\u00e9 final.pdf](/tmp/r\u00e9sum\u00e9 final.pdf)",
])
def test_streamed_markdown_attachment_is_not_spoken(reference):
    chunker = SentenceChunker()
    parts = []
    for char in "Done.\n" + reference + "\n\nAll done.":
        parts += chunker.feed(char)
    parts += chunker.flush()
    assert " ".join(part for part in map(_strip_markdown_for_tts, parts) if part) == "Done. All done."
