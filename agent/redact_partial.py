"""Credential repr fields left unterminated on a diagnostic line, for agent.redact.

The full-field pass needs the closing quote, so ``RuntimeError: {'API_KEY': 'abc…`` from a
process still streaming, or a line cut by a capture window, would keep the value verbatim.
"""

import re

# Only a value that runs to the end of the line can match: it may not contain its own quote.
_UNTERMINATED_REPR_FIELD_RE = re.compile(
    r"""(?P<kquote>['"])(?P<key>[A-Za-z_][A-Za-z0-9_]{0,99})(?P=kquote)\s*:\s*[bB]?(?P<quote>['"])"""
    r"""(?P<value>(?:\\.|(?!(?P=quote))[^\\])*)\Z""")


def redact_unterminated_repr_field(body: str) -> str:
    """Mask a trailing secret field value that never closed."""
    from agent.redact import is_secret_field_name

    match = _UNTERMINATED_REPR_FIELD_RE.search(body)
    if match is None or not is_secret_field_name(match["key"]):
        return body
    return body[:match.start("value")] + "***"
