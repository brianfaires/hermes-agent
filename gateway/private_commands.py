"""Private capture after identity/channel admission, before observers and sessions."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PrivateAdmission:
    """Process-local command identity retained across asynchronous profile setup."""
    home: Path
    text: str
    match: tuple


def pin_private_command(event, home, match):
    event._private_command_admission = PrivateAdmission(Path(home).resolve(), event._command_text(), match)


def private_home_for_source(source):
    from hermes_constants import get_hermes_home
    from hermes_cli.profiles import get_profile_dir

    return get_profile_dir(source.profile) if source.profile else get_hermes_home()


def admit_private_command(runner, event):
    from hermes_cli.private_commands import match_private_command, invoke_private_command

    admission = getattr(event, "_private_command_admission", None)
    admitted = isinstance(admission, PrivateAdmission)
    if not admitted and not event.get_command() and not event.reply_to_text:
        return False
    source = event.source
    home = runner._resolve_profile_home_for_source(source)
    if event.reply_to_text and match_private_command(event.reply_to_text, home=home):
        event.reply_to_text = None
    if not admitted and not event.get_command():
        return False
    match = match_private_command(event._command_text(), home=home)
    if match is None and not admitted:
        return False
    event._private_command_handled = True
    event._private_command_reply = None
    if event.internal or not runner._is_user_authorized_for_source(source):
        return True
    denied = runner._check_slash_access(source, admission.match[0] if admitted else match[0])
    if denied is not None:
        event._private_command_reply = denied
        return True
    if admitted and (Path(home).resolve() != admission.home or event._command_text() != admission.text
                     or match is None or match[1] is not admission.match[1]):
        event._private_command_reply = 'Command became unavailable; nothing was captured. Please retry.'
        return True
    from gateway.platforms.event import MessageType
    attachments = bool(event.media_urls or event.media_types or
                       event.message_type not in (MessageType.TEXT, MessageType.COMMAND))
    event._private_command_reply = invoke_private_command(
        match, home=home, platform=source.platform.value, attachments=attachments)
    return True


def private_platform_event(runner, event, source):
    from hermes_cli.private_commands import match_private_command
    payload = event.get('payload') or {}
    text = payload.get('text') or payload.get('content') or ''
    if event.get('platform') == 'discord':
        import re
        text = re.sub(r'^\s*<@!?[0-9]+>\s*', '', text, count=1)
    return match_private_command(text, home=runner._resolve_profile_home_for_source(source)) is not None
