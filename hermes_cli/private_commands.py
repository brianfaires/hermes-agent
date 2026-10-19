"""Explicit private plugin commands: dispatch before history, hooks or model work."""
from pathlib import Path

from prompt_toolkit.history import FileHistory

_FAILED = 'Private command failed; no conversation was started.'


def match_private_command(text, *, home=None):
    from hermes_constants import get_hermes_home
    from gateway.run import _profile_runtime_scope

    if not isinstance(text, str) or not text.lstrip().startswith('/'):
        return None
    text = text.lstrip()
    head = text.split(maxsplit=1)[0]
    name = head[1:].split('@', 1)[0].lower().replace('_', '-')
    from hermes_cli.commands import resolve_command
    if resolve_command(name) is not None:
        return None
    from hermes_cli.plugins import get_plugin_commands
    with _profile_runtime_scope(home or get_hermes_home()):
        entry = get_plugin_commands().get(name)
    if entry and entry.get('private'):
        return name, entry, text[len(head) + 1:] if len(text) > len(head) else ''
    return None


def invoke_private_command(match, *, home, platform, attachments=False):
    from gateway.run import _profile_runtime_scope
    from hermes_cli.plugins import resolve_plugin_command_result

    if attachments:
        return 'Private commands accept text only.'
    allowed = match[1].get('private_platforms')
    if allowed is not None and platform not in allowed:
        return 'Private command unavailable on this platform.'
    try:
        with _profile_runtime_scope(home):
            return str(resolve_plugin_command_result(match[1]['handler'](match[2], home=Path(home))) or '')
    except Exception:  # health: allow BLE001 -- Handler errors may contain private text; never log or return them.
        return _FAILED


def dispatch_cli_private(text, *, attachments=False):
    from hermes_constants import get_hermes_home

    match = match_private_command(text)
    if match is None:
        return None
    output = invoke_private_command(match, home=get_hermes_home(), platform='cli', attachments=attachments)
    from cli import _cprint
    _cprint(output)
    return output


def dispatch_cli_editor_private(cli, event):
    from hermes_constants import get_hermes_home

    buf = event.app.current_buffer
    match = match_private_command(buf.text)
    if match is None:
        return False
    output = invoke_private_command(match, home=get_hermes_home(), platform='cli',
                                    attachments=bool(cli._attached_images))
    cli._attached_images.clear()
    buf.reset(append_to_history=False)
    from cli import _cprint
    _cprint(output)
    event.app.invalidate()
    return True


class PrivateFileHistory(FileHistory):
    def append_string(self, string):
        if match_private_command(string) is None:
            super().append_string(string)
