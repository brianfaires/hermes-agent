import shlex

import pytest

from cron.lifecycle_guard import contains_gateway_lifecycle_command_or_referenced_script


def _scan(command: str) -> bool:
    return contains_gateway_lifecycle_command_or_referenced_script(command)


def test_python_heredoc_reading_oversized_json_is_not_a_shell_script(tmp_path):
    payload = tmp_path / "payload.json"
    payload.write_text('{"items":"' + "x" * (1024 * 1024 + 1) + '"}', encoding="utf-8")

    command = f'''python3 -B - <<'PY'
import json
import sqlite3
from pathlib import Path
payload = json.loads(Path({str(payload)!r}).read_text(encoding="utf-8"))
other = json.loads(Path({str(payload)!r}).read_text())
for item in payload.get("items", []):
    pass
connection = sqlite3.connect(":memory:")
connection.execute("SELECT 1").fetchall()
PY'''

    assert _scan(command) is False


def test_direct_gateway_lifecycle_command_remains_blocked():
    assert _scan("hermes gateway restart") is True


@pytest.mark.xfail(strict=True, reason="Baseline gap: quoted Python heredoc is stripped before direct argv scanning")
def test_python_subprocess_argv_lifecycle_remains_blocked():
    command = '''python3 - <<'PY'
import subprocess
subprocess.run(["hermes", "gateway", "restart"], check=True)
PY'''

    assert _scan(command) is True


def test_python_exec_of_read_text_code_remains_blocked(tmp_path):
    loaded_code = tmp_path / "loaded.py"
    loaded_code.write_text(
        'import subprocess\nsubprocess.run(["hermes", "gateway", "restart"])\n',
        encoding="utf-8",
    )
    command = f'''python3 -B - <<'PY'
from pathlib import Path
exec(Path({str(loaded_code)!r}).read_text(encoding="utf-8"))
PY'''

    assert _scan(command) is True


@pytest.mark.parametrize("statement", [
    "exec(Path({path}).read_text())",
    "text = Path({path}).read_text(); exec(text)",
    "text = Path({path}).read_text(); alias = text; exec(alias)",
    "text = Path({path}).read_text(); consume(text)",
    "text = Path({path}).read_text(); alias = text; consume(alias)",
    "subprocess.run({path})",
])
def test_python_references_keep_baseline_detection(tmp_path, statement):
    script = tmp_path / "loaded.json"
    script.write_text("hermes gateway restart\n", encoding="utf-8")
    command = (
        "python3 - <<'PY'\nimport json\nimport subprocess\nfrom pathlib import Path\n"
        + statement.format(path=repr(str(script))) + "\nPY\n"
    )
    assert _scan(command) is True


def test_data_read_does_not_exempt_another_execution_of_same_path(tmp_path):
    script = tmp_path / "clarifications.json"
    script.write_text("hermes gateway restart\n", encoding="utf-8")
    command = (
        "python3 - <<'PY'\nimport json\nfrom pathlib import Path\n"
        f"data = json.loads(Path({str(script)!r}).read_text())\n"
        f"exec(Path({str(script)!r}).read_text())\nPY\n"
    )
    assert _scan(command) is True


def test_nested_shell_payload_preserves_reference_detection(tmp_path):
    script = tmp_path / "restart.sh"
    script.write_text("hermes gateway restart\n", encoding="utf-8")
    command = (
        "python3 - <<'PY'\nimport json\nfrom pathlib import Path\n"
        f"data = json.loads(Path({str(script)!r}).read_text())\nPY\n"
        + "sh -c " + shlex.quote("bash " + shlex.quote(str(script)))
    )
    assert _scan(command) is True


@pytest.mark.parametrize("statement", [
    "Path({path}).read_text()",
    "data = json.loads(Path({path}).read_text())",
])
def test_supported_data_reads_skip_reference_io(tmp_path, statement):
    path = tmp_path / "data.without_json_suffix"
    reads = []
    command = (
        "python3 - <<'PY'\nimport json\nfrom pathlib import Path\n"
        + statement.format(path=repr(str(path))) + "\nPY\n"
    )
    assert contains_gateway_lifecycle_command_or_referenced_script(
        command, read_remote_script=lambda candidate: reads.append(candidate)
    ) is False
    assert str(path) not in reads


@pytest.mark.parametrize("prefix", [
    "Path = custom_path\n",
    "json.loads = custom_loads\n",
    "from custom import json\n",
])
def test_rebound_data_read_names_keep_baseline_scanning(tmp_path, prefix):
    path = tmp_path / "oversized.json"
    path.write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
    command = (
        "python3 - <<'PY'\nimport json\nfrom pathlib import Path\n"
        + prefix + f"data = json.loads(Path({str(path)!r}).read_text())\nPY\n"
    )
    assert _scan(command) is True


def test_python_shaped_text_outside_heredoc_keeps_baseline_scanning(tmp_path):
    path = tmp_path / "oversized.json"
    path.write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
    assert _scan(
        "import json\nfrom pathlib import Path\n"
        f"data = json.loads(Path({str(path)!r}).read_text())"
    ) is True


@pytest.mark.parametrize("lifecycle", [False, True])
def test_terminal_entrypoint_data_read_and_lifecycle_gate(monkeypatch, tmp_path, lifecycle):
    import json
    import tools.terminal_tool as tt
    from tests.hermes_cli.test_gateway_restart_loop import TestTerminalToolGatewayLifecycleGuard

    calls = []

    class FakeEnv:
        env = {}

        def execute(self, command, **kwargs):
            calls.append(command)
            return {"output": "fixture backend only", "returncode": 0}

    TestTerminalToolGatewayLifecycleGuard()._patch_env(
        monkeypatch, FakeEnv(), inside_gateway=True
    )
    monkeypatch.setattr(tt, "_check_all_guards", lambda *a, **kw: {"approved": True})
    data = tmp_path / "clarifications.json"
    data.write_text(json.dumps({"items": "x" * (1024 * 1024 + 1)}))
    command = (
        "/usr/bin/python3 -B - <<'PY'\nimport json\nfrom pathlib import Path\n"
        f"items = json.loads(Path({str(data)!r}).read_text())\nPY\n"
    )
    if lifecycle:
        command += "hermes gateway restart\n"
    result = json.loads(tt.terminal_tool(command=command))
    if lifecycle:
        assert result["exit_code"] == 1
        assert "Blocked" in result["error"]
        assert calls == []
    else:
        assert result["exit_code"] == 0
        assert calls[-1] == command
        assert calls.count(command) == 1
        # The terminal may probe the interpreter through its remote reader;
        # it must not probe the JSON data file as executable input.
        assert all(str(data) not in call for call in calls[:-1])


@pytest.mark.parametrize("api", ["create_subprocess_shell", "create_subprocess_exec"])
def test_asyncio_execution_of_json_data_keeps_reference_scan(tmp_path, api):
    import json

    payload = tmp_path / "async_command.json"
    payload.write_text(json.dumps("hermes gateway restart"), encoding="utf-8")
    command = (
        "python3 -B - <<'PY'\nimport asyncio, json\nfrom pathlib import Path\n"
        "loop = asyncio.get_event_loop()\n"
        f"loop.run_until_complete(asyncio.{api}(json.loads(Path({str(payload)!r}).read_text())))\n"
        "PY\n"
    )
    assert _scan(command) is True


def test_real_oversized_shell_script_still_fails_closed(tmp_path):
    script = tmp_path / "oversized.sh"
    script.write_text("#!/bin/sh\n" + "x" * (1024 * 1024 + 1), encoding="utf-8")

    assert _scan(f"bash {script}") is True
