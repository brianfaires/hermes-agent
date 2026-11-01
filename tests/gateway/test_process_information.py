"""Informational CLI invocations are never live gateway identities."""

import shlex
from pathlib import Path

import pytest

from gateway.status import looks_like_gateway_command_line


@pytest.mark.parametrize("args", [
    ["--help", "gateway", "run"],
    ["gateway", "run", "--help"],
    ["--print-runtime-command", "gateway", "run"],
])
@pytest.mark.parametrize("native_launcher", [False, True])
def test_information_command_is_not_a_running_gateway(args, native_launcher):
    def command(argv):
        if native_launcher:
            from hermes_cli._launchers import _launcher_script
            source = _launcher_script("hermes", Path("/opt/hermes-agent"), None)
            return " ".join(["python", "-I", "-c", source, *argv])
        return shlex.join(["hermes", *argv])

    assert not looks_like_gateway_command_line(command(args))
    assert looks_like_gateway_command_line(command(["gateway", "run"]))
