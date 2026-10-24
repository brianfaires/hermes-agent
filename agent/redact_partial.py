"""Incomplete credential assignments in truncated or still-streaming diagnostics."""
import re

QUOTED_ENV_ASSIGNMENT = re.compile(
    r'(?<![\w])([A-Za-z_][A-Za-z0-9_]{0,99})\s*=\s*'
    r'''(['"])((?:\\.|(?!\2)[^\r\n\\])*)(?:\2|(?=\r?$))''', re.MULTILINE)
_PARTIAL_REPR = re.compile(
    r'''(?P<kquote>['"])(?P<key>[A-Za-z_][A-Za-z0-9_]{0,99})(?P=kquote)'''
    r'''\s*:\s*[bB]?(?P<quote>['"])(?P<value>(?:\\.|(?!(?P=quote))[^\r\n\\])*)$''')


def redact_partial_repr(text):
    """Only called for high-confidence pytest/exception diagnostic lines."""
    from agent.redact import (
        is_secret_field_name, _assignment_sub, _ENV_ASSIGN_RE, _ENV_ASSIGN_LOWER_RE,
    )

    mask = _assignment_sub(lambda groups: f"{groups[0]}={groups[1]}***{groups[1]}", check_keyword=True)
    text = QUOTED_ENV_ASSIGNMENT.sub(mask, text)
    text = _ENV_ASSIGN_RE.sub(mask, text)
    text = _ENV_ASSIGN_LOWER_RE.sub(mask, text)

    match = _PARTIAL_REPR.search(text)
    if match is not None and is_secret_field_name(match['key']):
        return text[:match.start('value')] + '***'
    return text
