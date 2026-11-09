"""Background-process output selection: redact the complete retained capture, then cut.

A preview, tail, log page or watch excerpt cut from raw output can split a secret so that no
redaction pattern recognises the fragment left behind. Every reader therefore selects from
``redacted_capture``. Rolling-buffer eviction keeps only non-secret boundary state (an open
private-key block, a split first line) so the capture never starts inside a secret.
"""

import re
from contextlib import nullcontext

from tools.ansi_strip import strip_ansi

_PEM_BOUNDARY = re.compile(r"-----(BEGIN|END)[A-Z ]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END[A-Z ]*PRIVATE KEY-----")
PARTIAL_LINE_OMITTED = "[Partial output line omitted before redaction]"
WATCH_MATCH_OMITTED = "[Matched output omitted after redaction]"


def trim_output(session) -> None:
    """Evict the buffer to ``max_output_chars``, remembering whether the cut split a line or
    left a private-key block open. Called with ``session._lock`` held."""
    full = session.output_buffer
    cut = len(full) - session.max_output_chars
    if cut <= 0:
        return
    for match in _PEM_BOUNDARY.finditer(full):
        if match.start() >= cut:
            break
        session._output_pem_open = match[1] == "BEGIN"
    session._output_starts_midline = full[cut - 1] != "\n"
    session.output_buffer = full[cut:]


def reset_output(session) -> None:
    session.output_buffer = ""
    session._output_pem_open = session._output_starts_midline = False


def retained_output(session, *, final: bool = False) -> str:
    """The buffer as redaction must see it, never starting inside an evicted line or key and,
    while the process runs (and the caller is not reporting it ``final``), holding back the
    unfinished token of an unterminated last line.
    Called with ``session._lock`` held so the buffer and its eviction state pair up."""
    output = session.output_buffer
    # Duck-typed sessions (gateway rows, test doubles) carry no eviction state.
    if getattr(session, "_output_pem_open", False):
        end = _PEM_END.search(output)
        output = "[REDACTED PRIVATE KEY]" + (output[end.end():] if end else "")
    elif getattr(session, "_output_starts_midline", False):
        _, newline, output = output.partition("\n")
        if not newline:
            return PARTIAL_LINE_OMITTED
    # Only the trailing token is held: a prompt ending in whitespace (``Password: ``) stays readable.
    if output and not final and not output[-1].isspace() and not getattr(session, "exited", True):
        held = len(output.split()[-1])
        return output[:-held] + f"[{held} chars held until the line ends]"
    return output


def snapshot_output(session, *, final: bool = False) -> str:
    with getattr(session, "_lock", None) or nullcontext():
        return retained_output(session, final=final)


def redact_retained(session, retained: str, *, force: bool = False) -> str:
    """Redact a ``retained_output`` snapshot; callers take it under the lock, redact outside."""
    from agent.redact import redact_terminal_output

    return redact_terminal_output(strip_ansi(retained), getattr(session, "command", "") or "", force=force)


def redacted_capture(session, *, force: bool = False, final: bool = False) -> str:
    """Never call with ``session._lock`` held: it snapshots under that lock. ``final``: the
    caller reports the capture as the process's result, so nothing is held back."""
    return redact_retained(session, snapshot_output(session, final=final), force=force)


def output_tail(session, count: int, retained: str | None = None) -> str:
    if count <= 0:
        return ""
    text = redacted_capture(session) if retained is None else redact_retained(session, retained)
    return text[-count:]


def watch_output(session, chunk: str) -> str:
    """The watch excerpt for ``chunk``, taken from the redacted capture's matching lines: a
    chunk alone can hold only the tail of a secret, which no pattern recognises."""
    # A chunk checked before it reached the buffer is the whole capture.
    text = redacted_capture(session) if session.output_buffer else redact_retained(session, chunk)
    lines = text.splitlines()[-len(chunk.splitlines()):]
    matched = [line.rstrip() for line in lines if any(p in line for p in session.watch_patterns)]
    output = "\n".join(matched[-20:]) or WATCH_MATCH_OMITTED
    return output[:2000] + "\n...(truncated)" if len(output) > 2000 else output


def command_preview(command: str) -> str:
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(command, code_file=True)[:200]
