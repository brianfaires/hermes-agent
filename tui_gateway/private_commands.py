"""Session-free private command RPC dispatch with explicit profile ownership."""


def dispatch(server, rid, params, text, *, result_kind='private'):
    from hermes_constants import get_hermes_home
    from hermes_cli.private_commands import match_private_command, invoke_private_command

    from gateway.platforms.event import MessageEvent
    structured = not isinstance(text, str)
    if isinstance(text, list):
        text = '\n'.join(part.get('text', '') for part in text if isinstance(part, dict)
                         and isinstance(part.get('text'), str))
    if not isinstance(text, str):
        return None
    candidate = MessageEvent(text=text)._command_text()
    if not candidate.startswith('/'):
        return None
    attachments = structured or candidate != text.lstrip()
    sid = params.get('session_id')
    session = server._sessions.get(sid) if sid else None
    if sid and session is None:
        return server._err(rid, 4001, 'session not found')
    with server._session_home_scope(session, profile=params.get('profile')):
        home = get_hermes_home()
        match = match_private_command(candidate, home=home)
        if match is None:
            return None
        output = invoke_private_command(match, home=home, platform='tui',
            attachments=bool(attachments or params.get('attachments') or params.get('voice_turn') or params.get('images')))
    if result_kind == 'private':
        return server._ok(rid, dict(handled=True, output=output))
    if result_kind == 'prompt':
        return server._ok(rid, dict(private_command=True, output=output))
    if result_kind == 'slash':
        return server._ok(rid, dict(output=output))
    return server._ok(rid, dict(type='plugin', output=output))
