"""Explicit private plugin commands: dispatch before history, hooks or model work."""
import inspect
import re
from pathlib import Path

from prompt_toolkit.history import FileHistory

_FAILED = 'Private command failed; no conversation was started.'


def match_private_command(text, *, home=None):
    from hermes_constants import get_hermes_home

    if not isinstance(text, str) or not text.lstrip().startswith('/'):
        return None
    text = text.lstrip()
    head = text.split(maxsplit=1)[0]
    name = head[1:].split('@', 1)[0].lower().replace('_', '-')
    from hermes_cli.commands import resolve_command
    if resolve_command(name) is not None:
        return None
    from hermes_cli.plugins import get_plugin_commands
    from gateway.run import _profile_runtime_scope
    with _profile_runtime_scope(home or get_hermes_home()):
        entry = get_plugin_commands().get(name)
    if entry and entry.get('private'):
        return name, entry, text[len(head) + 1:] if len(text) > len(head) else ''
    return None


def invoke_private_command(match, *, home, platform, attachments=False, source=None):
    from gateway.run import _profile_runtime_scope
    from hermes_cli.plugins import resolve_plugin_command_result

    if attachments:
        return 'Private commands accept text only.'
    allowed = match[1].get('private_platforms')
    if allowed is not None and platform not in allowed:
        return 'Private command unavailable on this platform.'
    try:
        with _profile_runtime_scope(home):
            from hermes_cli.profiles import get_active_profile_name
            handler = match[1]['handler']
            kwargs = {'home': Path(home)}
            if 'source' in inspect.signature(handler).parameters:
                provenance = {'platform': platform, 'chat_type': 'dm',
                              'profile': get_active_profile_name()}
                provenance.update({key: value for key, value in (source or {}).items()
                                   if key in {'chat_type', 'profile'} and value is not None})
                kwargs['source'] = provenance
            return str(resolve_plugin_command_result(handler(match[2], **kwargs)) or '')
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
    text, files = expand_private_pastes(cli, buf.text)
    if text is None:
        output = 'Private paste is unavailable; nothing was captured. Please paste it again.'
    else:
        match = match_private_command(text)
        output = invoke_private_command(match, home=get_hermes_home(), platform='cli',
                                        attachments=bool(cli._attached_images))
        if output != _FAILED and not cli._attached_images:
            for path in files:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    import logging
                    logging.getLogger(__name__).warning('Private paste cleanup failed')
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


def expand_private_pastes(cli, text):
    """Resolve only this editor's own collapsed files; never acknowledge placeholders."""
    files = []
    known = getattr(cli, '_collapsed_paste_files', {})

    def expand(match):
        path = known.get(match.group(0))
        if path is None:
            raise ValueError('unknown paste')
        content = path.read_text(encoding='utf-8')  # windows-footgun: ok — Generated paste: a leading BOM is note content.
        files.append(path)
        return content

    try:
        return re.sub(r'\[Pasted text #\d+: \d+ lines → .+?\]', expand, text), files
    except (OSError, ValueError):
        return None, []


def dispatch_cli_query_private(query, *, image=None, output_format='text'):
    """One-shot capture runs before provider setup, agent creation or transcript output."""
    from hermes_cli.plugins import discover_plugins
    from hermes_constants import get_hermes_home
    if not isinstance(query, str) or not query.lstrip().startswith('/'):
        return False
    discover_plugins()
    match = match_private_command(query)
    if match is None:
        return False
    output = invoke_private_command(match, home=get_hermes_home(), platform='cli', attachments=bool(image))
    failed = output == _FAILED or bool(image)
    if output_format == 'stream-json':
        from hermes_cli.stream_json import StreamJsonEmitter
        StreamJsonEmitter(model='', session_id='').emit_result(
            {'failed': failed, 'final_response': output}, session_id='', exit_code=int(failed))
    else:
        print(output)
    if failed:
        raise SystemExit(1)
    return True


def dispatch_cli_startup_private(query, *, image=None, output_format='text', quiet=False,
                                 oneshot=False, list_tools=False, list_toolsets=False, use_tui=False):
    """Preserve literal interactive seeds; one-shot slash commands use direct capture."""
    if use_tui or list_tools or list_toolsets or not isinstance(query, str) or not query.lstrip().startswith('/'):
        return False
    from cli import _should_seed_interactive
    if _should_seed_interactive(query, image, quiet or output_format == 'stream-json', oneshot):
        return False
    return dispatch_cli_query_private(query, image=image, output_format=output_format)
