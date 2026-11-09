"""Redact a foreground capture over its complete text before the head/tail window is chosen.

``_BoundedOutputCollector.render`` cuts the stream to ``max_chars``; a cut through a secret
leaves a fragment no redaction pattern recognises. When a cut happened, the complete text comes
from memory (nothing evicted) or the spill file; when neither holds it, the output is omitted.
"""

from tools.tool_output_truncate import head_tail_split, truncation_notice

CONTEXT_UNAVAILABLE = "[Output omitted: complete redaction context is unavailable]"


def _complete_text(collector, spill: str | None) -> str | None:
    retained = collector.unevicted_text()
    if retained is not None:
        return retained
    if not spill or collector._spill_capped:
        return None
    try:
        with open(spill, encoding="utf-8-sig", errors="replace") as stream:
            text = stream.read(collector._SPILL_CAP_CHARS + 1)
    except OSError:
        return None
    return text if len(text) <= collector._SPILL_CAP_CHARS else None


def _fit(text: str, budget: int) -> str:
    """Head/tail cut whose notice counts against ``budget`` (render's contract)."""
    if len(text) <= budget:
        return text
    head, tail = head_tail_split(max(0, budget - len(truncation_notice(len(text), len(text)))))
    notice = truncation_notice(len(text) - head - tail, len(text))
    return (text[:head] + notice + (text[-tail:] if tail else ""))[:budget]


def _redacted_complete(collector, spill: str | None) -> str | None:
    from agent.redact import redact_terminal_output
    from tools.ansi_strip import strip_ansi

    complete = _complete_text(collector, spill)
    return None if complete is None else redact_terminal_output(strip_ansi(complete), collector.redaction_command)


def redact_before_selection(collector, rendered: str, spill: str | None) -> tuple[str, str | None]:
    """``(output, redacted complete text or None)``; the latter spares the spill a second pass."""
    from agent.redact import _redact_enabled

    suffix = collector.render_suffix
    budget = collector.max_chars - len(suffix)
    if budget <= 0 or collector.total_chars <= budget or not _redact_enabled():
        return rendered, None  # nothing was cut, or the suffix alone fills the window
    safe = _redacted_complete(collector, spill)
    return _fit(CONTEXT_UNAVAILABLE if safe is None else safe, budget) + suffix, safe


def redacted_window(collector) -> str:
    """The window so far for a yield-to-background handoff: the model reads it and the adopted
    session is seeded with it, so it is redacted over the complete text like a final result."""
    from agent.redact import _redact_enabled

    if collector.redaction_command is None or not _redact_enabled():
        return collector.render()
    safe = _redacted_complete(collector, collector.flush_spill())
    return _fit(CONTEXT_UNAVAILABLE if safe is None else safe, collector.max_chars)
