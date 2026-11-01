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


@pytest.mark.parametrize("argv", [
    ["python", "-m", "compileall", "-q", "-j", "0", "--", "gateway/run.py", "hermes_cli/main.py"],
    ["python", "-m", "compileall", "--", "desktop-gateway.py"],
    ["python", "-m", "pytest", "--", "hermes", "gateway", "run"],
    ["echo", "hermes", "gateway", "run"],
])
def test_program_arguments_are_not_gateway_entrypoints(argv):
    assert not looks_like_gateway_command_line(shlex.join(argv))


@pytest.mark.parametrize("argv", [
    ["python", "-Imgateway.run"],
    ["python", "-X", "gateway/run.py", "-m", "hermes_cli.main", "gateway", "run"],
    ["python", "-W", "gateway", "hermes_cli/main.py", "gateway", "run"],
    ["pythonw.exe", "-m", "hermes_cli.main", "gateway", "run"],
])
def test_python_options_preserve_the_selected_gateway_entrypoint(argv):
    assert looks_like_gateway_command_line(shlex.join(argv))
