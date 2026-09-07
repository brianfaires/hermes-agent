"""FC-37 synthetic CLI integration tests, using the current Hermes schema."""
import ast
import builtins
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/hindsight_history/cli.py"
spec = importlib.util.spec_from_file_location("fc37_cli", CLI)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
pytestmark = pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0,
                                reason="CLI requires an unprivileged POSIX owner")


def create_profile(path, *, name=None):
    path.mkdir(parents=True, mode=0o700)
    # Load the actual schema literal without importing application startup code.
    tree = ast.parse((ROOT / "hermes_state_common.py").read_text())
    schema = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "SCHEMA_SQL" for t in n.targets))
    with sqlite3.connect(path / "state.db") as db:
        db.executescript(schema)
        db.executemany("INSERT INTO sessions (id, source, started_at, profile_name) VALUES (?, 'cli', ?, ?)",
                       [("requested", 1, name), ("newer", 99, name)])
    (path / "state.db").chmod(0o600)
    return path


@pytest.fixture
def profile(tmp_path, monkeypatch):
    profile = create_profile(tmp_path / "profiles" / "alpha", name="alpha")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    return profile


def message(profile, session="requested", role="assistant", content=None, calls=None,
            call_id=None, name=None, active=1, compacted=0):
    with sqlite3.connect(profile / "state.db") as db:
        db.execute("INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, tool_name, "
                   "timestamp, active, compacted) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                   (session, role, content, calls, call_id, name, active, compacted))


def call(name="hindsight_recall", call_id="recall-1"):
    return json.dumps([{"id": call_id, "type": "function", "function": {
        "name": name, "arguments": '{"query":"synthetic-query"}'}}])


def invoke(profile, output, *extra):
    return cli.main(["--profile-home", str(profile), "--session", "requested",
                     "--output", str(output), *extra])


def test_actual_cli_recorded_only_isolated_private(profile, tmp_path):
    message(profile, calls=call(), active=0, compacted=1)
    message(profile, role="tool", call_id="recall-1", content="synthetic-evidence")
    message(profile, session="newer", calls=call(), content="OTHER-SESSION")
    message(profile, role="tool", name="hindsight_retain", content="REWOUND", active=0)
    other = create_profile(tmp_path / "profiles" / "beta", name="beta")
    message(other, role="tool", name="hindsight_recall", content="OTHER-PROFILE")
    output = tmp_path / "report.json"
    before = (profile / "state.db").read_bytes()
    proc = subprocess.run([sys.executable, str(CLI), "--profile-home", str(profile),
                           "--session", "requested", "--output", str(output)],
                          capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(ROOT)))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "private-report-written\n" and not proc.stderr
    report = json.loads(output.read_text())
    assert report["mode"] == "recorded evidence"
    assert report["evidence"]["events"][1]["content"] == "synthetic-evidence"
    assert len(report["evidence"]["events"]) == 2
    assert all(secret not in output.read_text() for secret in ("OTHER-SESSION", "OTHER-PROFILE", "REWOUND"))
    assert output.stat().st_mode & 0o777 == 0o600
    assert before == (profile / "state.db").read_bytes()
    assert sorted(p.name for p in profile.iterdir()) == ["state.db"]


@pytest.mark.parametrize("violation", ["context", "permissions", "owner", "symlink", "hardlink", "output"])
def test_authorization_before_database_read(profile, tmp_path, monkeypatch, violation):
    if violation == "context":
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles/beta"))
    elif violation == "permissions":
        profile.chmod(0o755)
    elif violation == "owner":
        monkeypatch.setattr(cli.os, "geteuid", lambda: os.getuid() + 1)
    elif violation == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(profile, target_is_directory=True)
        profile = alias
        monkeypatch.setenv("HERMES_HOME", str(alias))
    elif violation == "hardlink":
        os.link(profile / "state.db", tmp_path / "alias.db")
    else:
        (tmp_path / "out").symlink_to(profile / "state.db")
    reads = Mock(side_effect=AssertionError("must not read database bytes"))
    monkeypatch.setattr(cli.os, "read", reads)
    assert invoke(profile, tmp_path / "out") == 2
    reads.assert_not_called()


def test_session_denials_before_message_queries(profile, tmp_path, monkeypatch):
    # SQLite authorizer sees the real SELECT order and blocks accidental message reads.
    real_connect = sqlite3.connect
    message_reads = []
    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        def authorize(action, table, column, *unused):
            if action == sqlite3.SQLITE_READ and table == "messages":
                message_reads.append(column)
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        return conn
    with real_connect(profile / "state.db") as db:
        db.execute("UPDATE sessions SET profile_name='beta' WHERE id='requested'")
    monkeypatch.setattr(cli.sqlite3, "connect", connect)
    assert invoke(profile, tmp_path / "out") == 2
    assert message_reads == []
    assert cli.main(["--profile-home", str(profile), "--session", "absent", "--output", str(tmp_path / "absent")]) == 2
    assert message_reads == []


@pytest.mark.parametrize("state", ["empty", "missing", "corrupt"])
def test_existing_session_recovers_without_messages(profile, tmp_path, state):
    with sqlite3.connect(profile / "state.db") as db:
        if state == "missing":
            db.execute("DROP TABLE messages")
        elif state == "corrupt":
            db.execute("ALTER TABLE messages RENAME COLUMN tool_calls TO broken")
    output = tmp_path / "out"
    assert invoke(profile, output) == 0
    report = json.loads(output.read_text())
    assert report["session_record"] == "present"
    assert report["messages"] == state
    assert report["evidence"] == {"status": state, "events": []}
    assert report["automatic_activity"] == "unknown: not inferred"


@pytest.mark.parametrize("content,status", [(None, "missing"), ("", "empty"), ("synthetic", "recorded")])
def test_result_status(profile, tmp_path, content, status):
    message(profile, role="tool", name="hindsight_recall", content=content)
    output = tmp_path / "out"
    assert invoke(profile, output) == 0
    assert json.loads(output.read_text())["evidence"]["events"][0]["status"] == status


@pytest.mark.parametrize("calls", ["{broken", "{}", '[{"function":null}]',
                                     '[{"function":{"name":"hindsight_recall","arguments":"bad"}}]'])
def test_corrupt_evidence_is_not_empty(profile, tmp_path, calls):
    message(profile, calls=calls)
    output = tmp_path / "out"
    assert invoke(profile, output) == 0
    assert json.loads(output.read_text())["evidence"]["status"] == "corrupt"


@pytest.mark.parametrize("fresh", [False, True])
def test_no_backend_initialization_queries_or_mutations(profile, tmp_path, monkeypatch, fresh):
    forbidden = Mock(side_effect=RuntimeError("PRIVATE backend failure"))
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if "hindsight" in name or name in ("agent.memory_manager", "hermes_cli.config"):
            return forbidden(name)  # importing provider/SDK/config itself is forbidden
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    # Spies on every operation a provider/SDK could expose, including failure.
    from types import SimpleNamespace
    fake = SimpleNamespace(**{name: forbidden for name in (
        "initialize", "Hindsight", "recall", "arecall", "retain", "aretain_batch",
        "delete", "delete_bank", "import_memories", "handle_tool_call")})
    monkeypatch.setitem(sys.modules, "hindsight_client", fake)
    output = tmp_path / "out"
    assert invoke(profile, output, *(["--reconstruct"] if fresh else [])) == (3 if fresh else 0)
    result = json.loads(output.read_text())
    assert result["mode"] == "recorded evidence"
    assert ("refused" in result["reconstruction"]) is fresh
    assert "PRIVATE" not in output.read_text()
    forbidden.assert_not_called()


@pytest.mark.parametrize("failure", ["missing", "corrupt", "wal", "journal", "exception"])
def test_unavailable_is_content_free(profile, tmp_path, monkeypatch, capsys, failure):
    if failure == "missing":
        (profile / "state.db").unlink()
    elif failure == "corrupt":
        (profile / "state.db").write_bytes(b"PRIVATE broken database")
    elif failure in ("wal", "journal"):
        (profile / f"state.db-{failure}").write_text("PRIVATE uncheckpointed")
    else:
        monkeypatch.setattr(cli, "report", Mock(side_effect=RuntimeError("PRIVATE response")))
    assert invoke(profile, tmp_path / "out") == 2
    captured = capsys.readouterr()
    assert not captured.out and "PRIVATE" not in captured.err
    assert (tmp_path / "out").read_text() == ""


def test_required_explicit_scope_and_content_free_argument_errors(capsys):
    assert cli.main(["--unknown", "PRIVATE"]) == 2
    assert "PRIVATE" not in capsys.readouterr().err


def test_missing_and_ambiguous_results_are_honest(profile, tmp_path):
    message(profile, calls=call(call_id="missing-result"))
    message(profile, calls=call(call_id="reused"))
    message(profile, calls=call(call_id="reused"), active=0, compacted=1)
    message(profile, role="tool", call_id="reused", content="one persisted result")
    output = tmp_path / "out"
    assert invoke(profile, output) == 0
    events = json.loads(output.read_text())["evidence"]["events"]
    assert [e["result_status"] for e in events if e["kind"] == "tool_call"] == [
        "missing", "ambiguous", "ambiguous"]


def test_real_wal_is_refused_then_checkpointed_snapshot_is_readable(profile, tmp_path):
    db = sqlite3.connect(profile / "state.db")
    try:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        db.execute("INSERT INTO messages (session_id, role, content, tool_name, timestamp) "
                   "VALUES ('requested', 'tool', 'synthetic WAL result', 'hindsight_recall', 1)")
        db.commit()
        wal = profile / "state.db-wal"
        before = wal.read_bytes()
        assert invoke(profile, tmp_path / "refused") == 2
        assert wal.read_bytes() == before
    finally:
        db.close()  # synthetic fixture writer checkpoints on close; CLI never does
    before = (profile / "state.db").read_bytes()
    output = tmp_path / "checkpointed"
    assert invoke(profile, output) == 0
    assert json.loads(output.read_text())["evidence"]["events"][0]["content"] == "synthetic WAL result"
    assert (profile / "state.db").read_bytes() == before
