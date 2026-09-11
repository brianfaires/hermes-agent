"""Pure executable-position classifier for the test live-system guard.

No commands are executed here. This is conservative command inspection, not a
shell interpreter; actual PID/process-group fences remain in tests/conftest.py.
"""
import re
import shlex

_KILLERS = {'pkill', 'killall', 'taskkill', 'skill', 'fuser'}
_SHELLS = {'sh', 'bash', 'zsh', 'dash', 'ksh'}
_VALUE_OPTIONS = {
    'sudo': {'-u', '-g', '-h', '-p', '-C', '-T', '-R', '-D', '--user', '--group', '--host', '--prompt', '--chdir', '--chroot'},
    'env': {'-u', '--unset', '-C', '--chdir', '-S', '--split-string'},
    'timeout': {'-k', '--kill-after', '-s', '--signal'},
    'nice': {'-n', '--adjustment'},
    'ionice': {'-c', '--class', '-n', '--classdata', '-p', '--pid', '-P', '--pgid', '-u', '--uid'},
    'stdbuf': {'-i', '-o', '-e', '--input', '--output', '--error'},
    'flock': {'-w', '--wait', '-E', '--conflict-exit-code'},
    'xargs': {'-a', '--arg-file', '-d', '--delimiter', '-E', '-I', '-L', '-n', '-P', '-s', '--max-args', '--max-procs', '--max-chars', '--replace'},
    'nohup': set(), 'setsid': set(), 'command': set(), 'exec': set(),
}
_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=')


def _head(value):
    return value.rsplit('/', 1)[-1].rsplit('\\', 1)[-1].lower()


def _segments(script):
    try:
        lexer = shlex.shlex(script, posix=True, punctuation_chars=';&|()')
        lexer.whitespace_split = True
        lexer.commenters = ''
        tokens = list(lexer)
    except ValueError:
        tokens = script.split()
    segment = []
    for token in tokens:
        if token and all(c in ';&|()' for c in token):
            if segment:
                yield segment
            segment = []
        else:
            segment.append(token)
    if segment:
        yield segment


def _killer_commands(tokens, depth=0):
    if depth > 20:
        return
    index = 0
    while index < len(tokens):
        if _ASSIGNMENT.match(tokens[index]):
            index += 1
            continue
        head = _head(tokens[index])
        if head in _KILLERS:
            yield tokens[index:]
            return
        if head in _SHELLS:
            for option_index in range(index + 1, len(tokens) - 1):
                option = tokens[option_index]
                if option.startswith('-') and 'c' in option[1:] and not option.startswith('--'):
                    for part in _segments(tokens[option_index + 1]):
                        yield from _killer_commands(part, depth + 1)
                    return
            return
        if head not in _VALUE_OPTIONS:
            return
        index += 1
        while index < len(tokens):
            token = tokens[index]
            if token == '--':
                index += 1
                break
            if head == 'env' and _ASSIGNMENT.match(token):
                index += 1
                continue
            if not token.startswith('-'):
                break
            index += 1
            if token in _VALUE_OPTIONS[head] and index < len(tokens):
                if head == 'env' and token in {'-S', '--split-string'}:
                    for part in _segments(tokens[index]):
                        yield from _killer_commands(part, depth + 1)
                    return
                index += 1
        if head in {'timeout', 'flock'}:
            index += 1  # duration / lock file is an operand, not an executable
        if head == 'flock' and index < len(tokens) and tokens[index] in {'-c', '--command'}:
            if index + 1 < len(tokens):
                for part in _segments(tokens[index + 1]):
                    yield from _killer_commands(part, depth + 1)
            return


def is_process_killer(cmd):
    """Identify killer executables that could target the live Hermes gateway."""
    if isinstance(cmd, (bytes, bytearray)):
        cmd = bytes(cmd).decode(errors='replace')
    if isinstance(cmd, (list, tuple)):
        commands = [list(map(str, cmd))]
    elif isinstance(cmd, str):
        commands = _segments(cmd)
    else:
        return False
    for segment in commands:
        for invocation in _killer_commands(segment):
            low = ' '.join(invocation).lower()
            full = any(t == '--full' or t.startswith('--full=') or
                       (t.startswith('-') and not t.startswith('--') and 'f' in t[1:])
                       for t in invocation[1:])
            if 'hermes' in low or 'gateway' in low or ('python' in low and full):
                return True
    return False
