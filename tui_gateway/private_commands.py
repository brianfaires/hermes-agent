"""Private command RPC adapter. Reads runtime identity, never creates a session."""
from pathlib import Path
from hermes_constants import get_hermes_home
from hermes_cli.private_commands import match_private_command, invoke_private_command


def dispatch(server, rid, params, text):
    sid = params.get('session_id')
    session = server._sessions.get(sid) if sid else None
    home = (session or {}).get('profile_home') or get_hermes_home()
    match = match_private_command(text, home=home)
    if not match:
        return None
    if sid and session is None:
        return server._err(rid, 4001, 'session not found')
    output = invoke_private_command(match, home=Path(home))
    return server._ok(rid, {'handled': True, 'type': 'plugin', 'output': output})
