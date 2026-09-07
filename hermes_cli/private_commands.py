"""Early dispatch for explicitly private plugin commands, without session state."""
from pathlib import Path


def match_private_command(text, *, home=None):
    if home is not None:
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        token = set_hermes_home_override(home)
        try:
            return match_private_command(text)
        finally:
            reset_hermes_home_override(token)
    if not isinstance(text, str) or not text.startswith('/'):
        return None
    head = text.split(maxsplit=1)[0] if text.split() else ''
    name = head[1:].split('@', 1)[0].lower().replace('_', '-')
    from hermes_cli.commands import resolve_command
    if resolve_command(name) is not None:
        return None
    from hermes_cli.plugins import get_plugin_commands
    entry = get_plugin_commands().get(name)
    if entry and entry.get('private'):
        return name, entry, text[len(head) + 1:] if len(text) > len(head) else ''
    return None


def invoke_private_command(match, *, home):
    from hermes_cli.plugins import resolve_plugin_command_result
    try:
        return str(resolve_plugin_command_result(match[1]['handler'](match[2], home=Path(home))) or '')
    except Exception:
        # Never interpolate handler errors: even a filename/exception can carry
        # the submitted text. Never fall through to the ordinary agent path.
        return 'Private command failed; no conversation was started.'
