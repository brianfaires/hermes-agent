"""Preserve full retained context until output transforms/redaction precede selection."""
import re
from typing import Callable

from tools.ansi_strip import strip_ansi

_PEM_BOUNDARY = re.compile(r'-----(BEGIN|END)[A-Z ]*PRIVATE KEY-----')


class OutputSelection(str):
    """An internal string preview carrying the context needed to redact its cut."""
    source: str
    project: Callable[[str], str]

    def __new__(cls, source, project):
        if isinstance(source, cls):
            previous = source.project
            full = source.source
            def selected(text):
                return project(previous(text))
        else:
            full, selected = source, project
        value = str.__new__(cls, selected(full))
        value.source, value.project = full, selected
        return value


def capture_source(session):
    output = session.output_buffer
    prefix = getattr(session, "_output_pem_prefix", "")
    if prefix:
        return prefix + '\n' + output
    if getattr(session, "_output_starts_midline", False):
        _, separator, output = output.partition('\n')
        if not separator:
            return '[Partial output line omitted before redaction]'
    return output


def retain_context(session, full, cut):
    """Retain no secret bytes: only whether the evicted prefix opened a PEM block."""
    prefix = getattr(session, "_output_pem_prefix", "")
    for match in _PEM_BOUNDARY.finditer(full):
        if match.start() >= cut:
            break
        prefix = match[0] if match[1] == 'BEGIN' else ''
    session._output_pem_prefix = prefix
    session._output_starts_midline = full[cut - 1] != '\n'


def tail_output(session, count):
    return OutputSelection(strip_ansi(capture_source(session)), lambda text: text[-count:])


def line_output(source, offset, limit):
    def project(text):
        lines = text.splitlines()
        selected = lines[-limit:] if offset is None and limit > 0 else lines[(offset or 0):(offset or 0) + limit]
        return '\n'.join(selected)
    return OutputSelection(source, project)


def heartbeat_limit(output, count):
    return OutputSelection(output, lambda text: text[-count:])


def watch_output(lines, session, pattern):
    source = capture_source(session) if session.output_buffer else '\n'.join(lines)
    count = min(20, len(lines))
    def project(text):
        matched = [line for line in text.splitlines() if pattern in line]
        text = '\n'.join(matched[-count:]) or '[Matched output omitted after redaction]'
        return text[:2000] + '\n...(truncated)' if len(text) > 2000 else text
    return OutputSelection(source, project)


def redact_process_result(result):
    from agent.redact import redact_sensitive_text, redact_terminal_output
    from tools import process_registry as pr

    if not isinstance(result, dict):
        return result
    command = result.get('command') or ''
    task_id = str(result.get('task_id') or '')
    if not task_id and (session := pr.process_registry.get(str(result.get('session_id') or ''))) is not None:
        task_id = str(session.task_id or '')
    for key in ('output', 'output_preview'):
        if isinstance(value := result.get(key), str) and value:
            source = value.source if isinstance(value, OutputSelection) else value
            transformed = pr.transform_process_output(source, command=command,
                                                      returncode=result.get('exit_code'), task_id=task_id)
            safe = redact_terminal_output(strip_ansi(transformed), command)
            result[key] = value.project(safe) if isinstance(value, OutputSelection) else safe
    if isinstance(command, str) and command:
        result['command'] = redact_sensitive_text(command, code_file=True)
    return result


def command_preview(command):
    from agent.redact import redact_sensitive_text
    return redact_sensitive_text(command, code_file=True)[:200]


def redact_output_tail(session, limit):
    from agent.redact import redact_terminal_output

    return redact_terminal_output(strip_ansi(capture_source(session)), session.command)[-limit:]


def gateway_tail(session, limit):
    from agent.redact import redact_terminal_output
    from gateway.run import _redact_gateway_user_facing_secrets
    from tools.process_registry import transform_process_output

    output = strip_ansi(capture_source(session))
    if not output:
        return ''
    command = getattr(session, 'command', '') or ''
    output = transform_process_output(output, command=command, returncode=session.exit_code,
                                      task_id=getattr(session, 'task_id', '') or '')
    output = redact_terminal_output(strip_ansi(output), command, force=True)
    return _redact_gateway_user_facing_secrets(output)[-limit:]
