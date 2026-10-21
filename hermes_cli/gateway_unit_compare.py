"""Compare the generated unit's live settings after operator drop-ins."""
from pathlib import Path
import re
import shlex

# Dependencies accumulate and cannot be cleared by a drop-in. Command/status
# lists and installation lists can be reset with an empty assignment.
_DEPENDENCIES = frozenset({'After', 'Wants', 'Requires', 'Before', 'BindsTo', 'PartOf'})
_LISTS = _DEPENDENCIES | frozenset({
    'WantedBy', 'RequiredBy', 'ExecStart', 'ExecStartPre', 'ExecStartPost',
    'ExecReload', 'ExecStop', 'ExecStopPost', 'RestartForceExitStatus',
    'RestartPreventExitStatus', 'SuccessExitStatus', 'EnvironmentFile',
})


def assignments(text):
    """Read logical assignments, including systemd continuation lines."""
    section, pending = '', ''
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', ';')):
            continue
        line = pending + line
        pending = ''
        if line.endswith('\\'):
            pending = line[:-1] + ' '
        elif line.startswith('[') and line.endswith(']'):
            section = line[1:-1].strip()
        elif '=' in line:
            key, value = line.split('=', 1)
            yield section, key.strip(), value.strip()


def _apply(state, text):
    for section, key, value in assignments(text):
        slot = section, key
        if key == 'Environment':
            env = state.setdefault(slot, {})
            if not value:
                env.clear()
            else:
                for token in shlex.split(value):
                    if '=' in token:
                        name, content = token.split('=', 1)
                        env[name] = content
        elif key in _LISTS:
            values = state.setdefault(slot, [])
            if value:
                values.append(value)
            elif key not in _DEPENDENCIES:
                values.clear()
        else:
            state[slot] = value


def _dropins(unit_path: Path, system: bool):
    from hermes_cli import gateway as gw

    # The manager knows generic, hierarchy, runtime and vendor drop-ins too.
    # Local files also cover offline setup before the first daemon-reload.
    properties = gw._systemctl_show(('DropInPaths',), system=system)
    paths = {Path(re.sub(r'\\x([0-9a-fA-F]{2})',
                         lambda m: chr(int(m[1], 16)), token))
             for token in properties.get('DropInPaths', '').split()}
    paths.update(unit_path.with_name(unit_path.name + '.d').glob('*.conf'))
    return sorted(paths)


def live_unit_is_current(installed: str, expected: str, unit_path: Path, system: bool) -> bool:
    """Apply identical overrides to both bases; compare only surviving values.

    Unreadable or malformed overrides fail conservatively. No unit is written
    here, and the existing service ownership gate still controls repair.
    """
    from hermes_cli import gateway as gw

    states = [{}, {}]
    try:
        for state, text in zip(states, (installed, expected)):
            _apply(state, gw._strip_optional_systemd_directives(text))
        for path in _dropins(unit_path, system):
            text = path.read_text(encoding='utf-8-sig')
            for state in states:
                _apply(state, text)
    except (OSError, UnicodeError, ValueError):
        return False
    return states[0] == states[1]
