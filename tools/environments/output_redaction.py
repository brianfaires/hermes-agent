"""Recover complete bounded foreground spill context before presenting a cut."""
import os
import stat


def redact_spilled_output(collector, rendered, spill):
    from agent.redact import _redact_enabled, redact_terminal_output
    from tools.ansi_strip import strip_ansi
    from tools.tool_output_truncate import truncate_head_tail

    if collector.total_chars <= collector.max_chars or not _redact_enabled():
        return rendered
    suffix = collector._render_suffix
    if len(suffix) >= collector.max_chars:
        return rendered
    budget = collector.max_chars - len(suffix)
    omitted = truncate_head_tail('[Output omitted: complete redaction context is unavailable]', budget)
    if not spill or collector._spill_capped:
        return omitted + suffix
    try:
        descriptor = os.open(spill, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, encoding='utf-8-sig', errors='replace') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return omitted + suffix
            raw = stream.read(collector._SPILL_CAP_CHARS + 1)
        if len(raw) > collector._SPILL_CAP_CHARS:
            return omitted + suffix
        safe = redact_terminal_output(strip_ansi(raw), collector.redaction_command)
        return truncate_head_tail(safe, budget) + suffix
    except OSError:
        return omitted + suffix
