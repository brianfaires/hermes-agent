from __future__ import annotations

from gateway.kanban_mirror.writer import PROSE_SYSTEM


def test_prose_prompt_tells_writer_how_to_reference_review_docs():
    assert "attached to the Discord thread" in PROSE_SYSTEM
    assert "never MEDIA: tags" in PROSE_SYSTEM
