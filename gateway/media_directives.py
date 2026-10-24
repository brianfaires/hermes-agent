"""Executable standalone media controls, separate from historical path hints."""
import os
from pathlib import Path
import re

_LINE = re.compile(r'''^[ \t]*(?:[-*][ \t]+)?(?P<wrap>[`"'*_]{0,3})MEDIA:[ \t]*(?P<raw>[^\n]*)$''',
                   re.IGNORECASE | re.MULTILINE)
_FENCE = re.compile(r'^[ \t]{0,3}(`{3,}|~{3,})(.*)$')
_CONTROLS = re.compile(r'\[\[(?:audio_as_voice|as_document)\]\]')


def fenced_spans(text):
    """Closed or unfinished Markdown fences, with matching delimiter/length."""
    spans, opened, offset = [], None, 0
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line.rstrip('\r\n'))
        if match and opened is None:
            opened = (offset, match[1][0], len(match[1]))
        elif (match and opened and match[1][0] == opened[1]
              and len(match[1]) >= opened[2] and not match[2].strip()):
            spans.append((opened[0], offset + len(line)))
            opened = None
        offset += len(line)
    if opened:
        spans.append((opened[0], len(text)))
    return spans


def _protected_scan(text):
    from gateway.platforms import base

    spans = [match.span() for match in _CONTROLS.finditer(text)]
    sentinel = base._terminal_sentinel_start(text)
    if sentinel >= 0:
        spans.append((sentinel, len(text.rstrip())))
    original = base._blank_spans(text, spans)
    masked = base.BasePlatformAdapter._mask_json_string_media(
        base.BasePlatformAdapter._mask_protected_spans(original))
    return original, masked


def masked_scan(text):
    """Mask original non-control lines before protected text can promote a tag."""
    from gateway.platforms import base

    original, masked = _protected_scan(text)
    rejected, offset = [], 0
    for raw, protected in zip(original.splitlines(keepends=True), masked.splitlines(keepends=True)):
        if _LINE.fullmatch(raw.rstrip('\r\n')) is None or raw != protected:
            rejected.append((offset, offset + len(raw)))
        offset += len(raw)
    return base._blank_spans(masked, rejected)


def _path_from_line(match):
    from gateway.platforms import base

    raw = match['raw'].strip()
    wrap = match['wrap']
    if 'media:' in raw.lower():
        return None
    if wrap:
        raw = raw.removesuffix('.').rstrip()
        closing = wrap[::-1]
        if not raw.endswith(closing):
            return None
        raw = raw[:-len(closing)].rstrip()
    quoted = len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in '`"\''
    path = base._normalize_media_tag_path(raw)
    if not path or (not quoted and not re.match(r'(?:~/|/|[A-Za-z]:[/\\])', path)):
        return None
    known = Path(path).suffix.lower() in base.MEDIA_DELIVERY_EXTS
    # A spaced bare string is ambiguous with prose: the complete file is the oracle.
    if not known or (not quoted and any(char.isspace() for char in path)):
        return base.validate_media_delivery_path(path)
    return path


def tag_matches(masked):
    """Yield (start, end, path) for complete, executable directive lines."""
    for match in _LINE.finditer(masked):
        if (path := _path_from_line(match)) is not None:
            yield match.start(), match.end(), path


def historical_paths(text):
    """Legacy token grammar supplies dedup hints only; it never requests delivery."""
    from gateway.platforms import base

    _, masked = _protected_scan(text)
    paths = {os.path.expanduser(base._normalize_media_tag_path(match['path']))
             for match in base.MEDIA_TAG_CLEANUP_RE.finditer(masked)}
    paths.update(path for _, path, _ in base._extensionless_media_matches(masked))
    return paths


def extract_local_files(content):
    """Keep the bare-file fallback for complete standalone paths, including spaces."""
    from gateway.platforms import base

    protected = base._code_spans(content)
    found, spans, offset = {}, [], 0
    for line in content.splitlines(keepends=True):
        candidate = re.sub(r'^[-*][ \t]+', '', line.strip())
        if (re.match(r'(?:~/|/|[A-Za-z]:[/\\])', candidate)
                and Path(candidate).suffix.lower() in base.MEDIA_DELIVERY_EXTS
                and not any(start < offset + len(line) and end > offset for start, end in protected)):
            expanded = os.path.expanduser(candidate)
            if os.path.isfile(expanded):
                found.setdefault(expanded, None)
                spans.append((offset, offset + len(line)))
        offset += len(line)
    if not found:
        return [], content
    return list(found), re.sub(r'\n{3,}', '\n\n', base._delete_spans(content, spans)).strip()
