"""Hermes CLI argv parsing for gateway process identity."""


def _python_program_argv(tokens: list[str]) -> list[str]:
    """Selected module/script and its arguments, excluding Python options and their operands."""
    from hermes_state_holders import (
        _PYTHON_LONG_OPTIONS_WITH_OPERANDS, _PYTHON_SHORT_OPTIONS_WITH_OPERANDS,
    )

    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1:]
        if token in _PYTHON_LONG_OPTIONS_WITH_OPERANDS:
            index += 2
        elif token.startswith("--"):
            index += 1
        elif token.startswith("-") and token != "-":
            cluster = token[1:]
            for position, letter in enumerate(cluster):
                attached = cluster[position + 1:]
                if letter == "c":
                    return []  # Only recognized in-process bootstraps are peeled by the caller.
                if letter == "m":
                    return [attached, *tokens[index + 1:]] if attached else tokens[index + 1:]
                if letter in _PYTHON_SHORT_OPTIONS_WITH_OPERANDS:
                    index += 1 if attached else 2
                    break
            else:
                index += 1
        else:
            return tokens[index:]
    return []


def gateway_process_subcommand(tokens: list[str]) -> str | None:
    """Bind identity to the program being executed; file/command operands are only data."""
    from hermes_state_holders import _looks_like_python_executable

    # Legacy runtime records may render the owning home as a leading shell assignment.
    while tokens and "=" in tokens[0] and tokens[0].split("=", 1)[0].isidentifier():
        tokens = tokens[1:]
    if not tokens:
        return None
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    program = (_python_program_argv(tokens)
               if _looks_like_python_executable(executable) or executable in {"pythonw", "pythonw.exe"}
               else tokens)
    if not program:
        return None
    entry = program[0].lower()
    name = entry.rsplit("/", 1)[-1]
    if (entry == "gateway.run" or entry == "gateway/run.py" or entry.endswith("/gateway/run.py")
            or name in {"desktop-gateway.py", "hermes-gateway", "hermes-gateway.exe"}):
        return "run"
    if not (name in {"hermes", "hermes.exe"} or entry == "hermes_cli.main"
            or entry == "hermes_cli/main.py" or entry.endswith("/hermes_cli/main.py")):
        return None
    return gateway_cli_subcommand([token.lower() for token in program[1:]])


def gateway_cli_subcommand(tokens: list[str]) -> str | None:
    # Profile selectors consume a value, even when that value happens to be "gateway".
    filtered = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
        elif token in ("--profile", "-p"):
            skip_next = True
        elif not token.startswith(("--profile=", "-p=")):
            filtered.append(token)
    # These invocations print information and exit before running a gateway. Inspect argv after
    # peeling inline launchers, so flag literals inside the launcher's source are never consulted.
    if {"--help", "-h", "--print-runtime-command"}.intersection(filtered):
        return None
    for index, token in enumerate(filtered):
        if token == "gateway":
            return filtered[index + 1] if index + 1 < len(filtered) else "run"
    return None
