"""Static classification controls: embedded Python is never executed."""

import json

import pytest

from cron.lifecycle_guard import _mask_python_heredoc_data_paths


@pytest.fixture
def data_path(tmp_path):
    path = tmp_path / "fictional_inventory.json"
    path.write_text(json.dumps("fictional catalog entry"), encoding="utf-8")
    return path


def command_for(path, statement, interpreter="python3 -B -"):
    return (
        f"{interpreter} <<'PY'\n"
        "from pathlib import Path\n"
        "import json\n"
        + statement.replace("READ", f"Path({str(path)!r}).read_text()")
        + "\nPY\n"
    )


@pytest.mark.parametrize("statement", ["READ", "data = json.loads(READ)"])
def test_actual_b_header_exempts_oversized_data_read(data_path, statement):
    data_path.write_text(json.dumps("fictional " * (1024 * 1024)), encoding="utf-8")
    command = command_for(data_path, statement)
    masked = _mask_python_heredoc_data_paths(command)
    assert masked == command.replace(repr(str(data_path)), "'_guard_data_'")


@pytest.mark.parametrize("flags", ["", "-B", "-I", "-u", "-E", "-s", "-S", "-B -I -u -E -s -S"])
def test_explicit_boolean_flags(data_path, flags):
    command = command_for(data_path, "data = json.loads(READ)", f"python3 {flags} -")
    assert str(data_path) not in _mask_python_heredoc_data_paths(command)


@pytest.mark.parametrize("interpreter", [
    "python3 -c pass -",
    "python3 -m fictional -",
    "python3 fictional.py -",
    "python3 -B - fictional_argument",
    "python3 -W ignore -",
    "python3 -Bu -",
])
def test_uncertain_interpreter_forms_keep_original_scanning(data_path, interpreter):
    command = command_for(data_path, "data = json.loads(READ)", interpreter)
    assert _mask_python_heredoc_data_paths(command) == command


@pytest.mark.parametrize("statement", [
    "exec(json.loads(READ))",
    "decoded = json.loads(READ)\nalias = decoded\nexec(alias)",
    "import subprocess\nsubprocess.run(json.loads(READ))",
    "import subprocess\ndecoded = json.loads(READ)\nalias = decoded\nsubprocess.run(alias)",
    "execute = exec\nexecute(json.loads(READ))",
    "from builtins import exec as execute\nexecute(json.loads(READ))",
    "import builtins\nexecute = getattr(builtins, 'exec')\nexecute(json.loads(READ))",
    "execute = __builtins__['exec']\nexecute(json.loads(READ))",
    "import subprocess as process\nlaunch = process.run\nlaunch(json.loads(READ))",
    "import os\nlaunch = os.system\nlaunch(json.loads(READ))",
    "import runpy\nrunpy.run_path(json.loads(READ))",
    "decoded = json.loads(READ)\ncompile(decoded, '<fictional>', 'exec')",
    "decoded = json.loads(READ)\neval(decoded)",
    "READ\nexec('pass')",
])
def test_execution_references_keep_entire_original_body(data_path, statement):
    command = command_for(data_path, statement)
    assert _mask_python_heredoc_data_paths(command) == command


@pytest.mark.parametrize("statement", [
    "raw = READ",
    "consume(READ)",
    "decode = json.loads\ndata = decode(READ)",
    "json.loads = replacement\ndata = json.loads(READ)",
    "Path = replacement\ndata = json.loads(READ)",
    "def load():\n    return json.loads(READ)",
])
def test_uncertain_data_forms_keep_original_scanning(data_path, statement):
    command = command_for(data_path, statement)
    assert _mask_python_heredoc_data_paths(command) == command


def test_inventory_operations_allow_bounded_data_read(data_path, tmp_path):
    output = tmp_path / "fictional_output.json"
    statement = (
        "import os\n"
        "import collections\n"
        "import psycopg2\n"
        "os.umask(0o077)\n"
        "connection = psycopg2.connect(dbname='fictional_inventory')\n"
        "query = connection.cursor()\n"
        "query.execute('SELECT 1')\n"
        "counts = collections.Counter()\n"
        "data = json.loads(READ)\n"
        f"Path({str(output)!r}).write_text(json.dumps(data))"
    )
    command = command_for(data_path, statement)
    masked = _mask_python_heredoc_data_paths(command)
    assert masked == command.replace(repr(str(data_path)), "'_guard_data_'")
    assert str(output) in masked
