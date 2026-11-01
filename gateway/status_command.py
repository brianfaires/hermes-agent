"""Hermes CLI argv parsing for gateway process identity."""


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
