"""FC-37 synthetic CLI integration tests, using the current Hermes schema."""
import builtins
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import Mock
from types import SimpleNamespace
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from hermes_state_common import SCHEMA_SQL

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/hindsight_history/cli.py"
spec = importlib.util.spec_from_file_location("fc37_cli", CLI)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
pytestmark = [pytest.mark.linux_only,
              pytest.mark.skipif(os.name == "posix" and os.geteuid() == 0,
                                 reason="CLI requires an unprivileged owner")]


def create_profile(path, *, name=None):
    path.mkdir(parents=True, mode=0o700)
    with sqlite3.connect(path / "state.db") as db:
        db.executescript(SCHEMA_SQL)
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


def test_default_never_loads_config_or_backend(profile, tmp_path, monkeypatch):
    forbidden = Mock(side_effect=RuntimeError("PRIVATE backend failure"))
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if "hindsight" in name or name in ("agent.memory_manager", "hermes_cli.config"):
            return forbidden(name)  # importing provider/SDK/config itself is forbidden
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    # Spies on every operation a provider/SDK could expose, including failure.
    fake = SimpleNamespace(**{name: forbidden for name in (
        "initialize", "Hindsight", "recall", "arecall", "retain", "aretain_batch",
        "delete", "delete_bank", "import_memories", "handle_tool_call")})
    monkeypatch.setitem(sys.modules, "hindsight_client", fake)
    output = tmp_path / "out"
    monkeypatch.setattr(cli, "reconstruction_config", forbidden)
    assert invoke(profile, output) == 0
    result = json.loads(output.read_text())
    assert result["mode"] == "recorded evidence"
    assert result["reconstruction"] == "not requested"
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


def test_actual_cli_reads_committed_live_wal_without_source_mutation(profile, tmp_path):
    with closing(sqlite3.connect(profile / "state.db")) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("INSERT INTO messages (session_id, role, content, tool_name, timestamp) "
                   "VALUES ('requested', 'tool', 'synthetic WAL result', 'hindsight_recall', 1)")
        db.execute("INSERT INTO messages (session_id, role, content, tool_name, timestamp) "
                   "VALUES ('newer', 'tool', 'OTHER-SESSION', 'hindsight_recall', 1)")
        db.commit()
        # immutable deliberately ignores WAL: prove this evidence lives in WAL.
        with closing(sqlite3.connect((profile / "state.db").as_uri() + "?immutable=1", uri=True)) as offline:
            assert offline.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                  for p in profile.iterdir()}
        assert before["state.db-wal"][0] and before["state.db-shm"][0]
        output = tmp_path / "wal-report"
        proc = subprocess.run([sys.executable, str(CLI), "--profile-home", str(profile),
                               "--session", "requested", "--output", str(output)],
                              capture_output=True, text=True)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "private-report-written\n", "")
        result = json.loads(output.read_text())
        assert result["evidence"]["events"][0]["content"] == "synthetic WAL result"
        assert "OTHER-SESSION" not in output.read_text()
        assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                          for p in profile.iterdir()}


def configure(profile, **overrides):
    directory = profile / "hindsight"
    directory.mkdir(mode=0o700, exist_ok=True)
    config = {"mode": "local_external", "api_url": "http://synthetic.invalid",
              "bank_id_template": "hermes-{profile}"}
    config.update(overrides)
    path = directory / "config.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    return path


def fact(**overrides):
    values = {"id": "fact-1", "text": "synthetic recovered fact", "type": "world",
              "tags": ["session:requested"],
              "metadata": {"session_id": "requested", "agent_identity": "alpha"}}
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def sdk(monkeypatch):
    forbidden = Mock(side_effect=AssertionError("PRIVATE mutation"))
    client = SimpleNamespace(recall=Mock(return_value=SimpleNamespace(results=[fact()])))
    for name in ("initialize", "retain", "aretain_batch", "delete", "delete_bank",
                 "import_memories", "create_bank", "reflect", "handle_tool_call"):
        setattr(client, name, forbidden)
    class Hindsight:
        def __init__(self, base_url, api_key=None, timeout=300):
            assert base_url == "http://synthetic.invalid"
        def recall(self, **kwargs):
            return client.recall(**kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *unused):
            pass
    factory = Mock(side_effect=Hindsight)
    factory.recall = Hindsight.recall
    monkeypatch.setitem(sys.modules, "hindsight_client", SimpleNamespace(Hindsight=factory))
    yield SimpleNamespace(client=client, factory=factory)
    forbidden.assert_not_called()


@pytest.mark.parametrize("state", ["empty", "missing", "corrupt", "recorded"])
def test_positive_reconstruction_with_authorized_session(profile, tmp_path, sdk, state):
    configure(profile)
    with closing(sqlite3.connect(profile / "state.db")) as db:
        if state == "missing":
            db.execute("DROP TABLE messages")
        elif state == "corrupt":
            db.execute("ALTER TABLE messages RENAME COLUMN tool_calls TO broken")
    if state == "recorded":
        message(profile, role="tool", name="hindsight_recall", content="original evidence")
    output = tmp_path / "reconstructed"
    assert invoke(profile, output, "--reconstruct") == 0
    report = json.loads(output.read_text())
    assert report["messages"] == state
    assert report["automatic_activity"] == "unknown: not inferred"
    fresh = report["reconstruction"]
    assert fresh["status"] == "reconstructed"
    assert fresh["label"] == "reconstructed from current memory NOT original transcript"
    assert fresh["facts"][0]["text"] == "synthetic recovered fact"
    assert fresh["facts"][0]["label"] == fresh["label"]
    sdk.client.recall.assert_called_once()
    args = sdk.client.recall.call_args.kwargs
    assert args["bank_id"] == "hermes-alpha"
    assert args["tags"] == ["session:requested"] and args["tags_match"] == "all_strict"
    assert args["types"] == ["world", "experience"]
    assert not any(args[k] for k in ("include_entities", "include_chunks", "include_source_facts", "trace"))
    assert "synthetic recovered fact" not in json.dumps(report["evidence"])


@pytest.mark.parametrize("template", [None, "hermes", "hermes-{user}", "hermes-{session}",
    "hermes-{profile!r}", "hermes-{profile:.3}", "hermes-{profile}-{workspace}",
    "hermes-{profile.__class__}", "hermes-{profile}-{profile}", "hermes--{profile}"])
def test_ambiguous_bank_never_queries(profile, tmp_path, sdk, template):
    configure(profile, bank_id_template=template, bank_id="hermes-alpha")
    output = tmp_path / "denied"
    assert invoke(profile, output, "--reconstruct") == 3
    fresh = json.loads(output.read_text())["reconstruction"]
    assert fresh["facts"] == [] and fresh["error"] == "reconstruction-profile-bank-not-isolated"
    sdk.factory.assert_not_called()


@pytest.mark.parametrize("violation", ["missing-session", "wrong-profile", "wrong-context"])
def test_reconstruction_scope_authorized_before_config_or_sdk(profile, tmp_path, monkeypatch, sdk, violation):
    if violation == "wrong-context":
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "elsewhere"))
    else:
        with closing(sqlite3.connect(profile / "state.db")) as db:
            if violation == "missing-session":
                db.execute("DELETE FROM sessions WHERE id='requested'")
            else:
                db.execute("UPDATE sessions SET profile_name='beta' WHERE id='requested'")
            db.commit()
    reads = Mock(side_effect=AssertionError("PRIVATE config"))
    monkeypatch.setattr(cli, "reconstruction_config", reads)
    assert invoke(profile, tmp_path / "denied", "--reconstruct") == 2
    reads.assert_not_called()
    sdk.factory.assert_not_called()


@pytest.mark.parametrize("change", [
    {"tags": None}, {"tags": []}, {"tags": ["session:requested-suffix"]},
    {"tags": ["session:requested", "session:newer"]}, {"tags": ["parent:requested"]},
    {"metadata": None}, {"metadata": {"session_id": "newer", "agent_identity": "alpha"}},
    {"metadata": {"session_id": "requested", "agent_identity": "beta"}},
    {"metadata": {"session_id": "requested"}}, {"type": "observation"},
    {"type": "opinion"}, {"type": None}, {"source_fact_ids": ["other-session-fact"]},
])
def test_response_provenance_fails_closed_for_entire_response(profile, tmp_path, sdk, change):
    configure(profile)
    sdk.client.recall.return_value = SimpleNamespace(results=[fact(), fact(text="PRIVATE", **change)])
    output = tmp_path / "denied"
    assert invoke(profile, output, "--reconstruct") == 3
    fresh = json.loads(output.read_text())["reconstruction"]
    assert fresh["error"] == "reconstruction-provenance-denied" and fresh["facts"] == []
    assert "PRIVATE" not in output.read_text() and "synthetic recovered fact" not in output.read_text()
    sdk.client.recall.assert_called_once()


@pytest.mark.parametrize("field", ["entities", "chunks", "source_facts", "trace"])
def test_synthesized_response_extras_are_not_exported(profile, tmp_path, sdk, field):
    configure(profile)
    sdk.client.recall.return_value = SimpleNamespace(results=[fact()], **{field: {"PRIVATE": "PRIVATE"}})
    output = tmp_path / "denied"
    assert invoke(profile, output, "--reconstruct") == 3
    assert json.loads(output.read_text())["reconstruction"]["facts"] == []
    assert "PRIVATE" not in output.read_text()


@pytest.mark.parametrize("failure", ["missing-config", "broken-config", "symlink-config", "permissions", "sdk", "backend"])
def test_reconstruction_unavailable_content_free(profile, tmp_path, monkeypatch, sdk, capsys, failure):
    path = configure(profile)
    if failure == "missing-config":
        path.unlink()
    elif failure == "broken-config":
        path.write_text("PRIVATE broken config")
    elif failure == "symlink-config":
        path.rename(path.with_name("elsewhere"))
        path.symlink_to(path.with_name("elsewhere"))
    elif failure == "permissions":
        path.chmod(0o644)
    elif failure == "sdk":
        monkeypatch.setitem(sys.modules, "hindsight_client", None)
    else:
        def fail(**kwargs):
            import logging
            logging.error("PRIVATE log")
            print("PRIVATE stdout")
            print("PRIVATE stderr", file=sys.stderr)
            raise RuntimeError("PRIVATE backend response")
        sdk.client.recall.side_effect = fail
    output = tmp_path / "unavailable"
    assert invoke(profile, output, "--reconstruct") == 3
    fresh = json.loads(output.read_text())["reconstruction"]
    assert fresh["status"] == "unavailable" and fresh["facts"] == []
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err + output.read_text()
    if failure == "backend":
        sdk.client.recall.assert_called_once()
    else:
        sdk.client.recall.assert_not_called()


def test_empty_current_memory_is_distinct_from_unavailable(profile, tmp_path, sdk):
    configure(profile)
    sdk.client.recall.return_value = SimpleNamespace(results=[])
    output = tmp_path / "empty"
    assert invoke(profile, output, "--reconstruct") == 0
    fresh = json.loads(output.read_text())["reconstruction"]
    assert fresh["status"] == "empty" and fresh["facts"] == [] and "error" not in fresh
    sdk.client.recall.assert_called_once()


@pytest.mark.parametrize("scenario", ["positive", "backend-error", "wrong-session"])
def test_installed_sdk_actual_cli_with_synthetic_transport(profile, tmp_path, scenario):
    """Run the actual optional SDK (including serialization) against a local fake.

    FC37_SDK_PYTHON can select an existing interpreter read-only; never install it.
    All HTTP requests terminate in this synthetic server, with no live data.
    """
    python = os.environ.get("FC37_SDK_PYTHON", sys.executable)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT))
    probe = subprocess.run([python, "-c", "import hindsight_client"], env=env,
                           capture_output=True, text=True)
    if probe.returncode:
        pytest.skip("optional Hindsight SDK is not installed in the test interpreter")
    with closing(sqlite3.connect(profile / "state.db")) as db:
        db.execute("DROP TABLE messages")
    requests = []
    class Transport(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.command, self.path, body, self.headers.get("Authorization")))
            payload = {"results": [vars(fact()), vars(fact(id="fact-2", type="experience"))]}
            code = 200
            if scenario == "backend-error":
                code, payload = 503, {"detail": "PRIVATE backend failure"}
            elif scenario == "wrong-session":
                payload["results"][0].update(tags=["session:newer"], text="PRIVATE other session")
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Transport)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        configure(profile, mode="cloud", api_url=f"http://127.0.0.1:{server.server_port}")
        secret = profile / ".env"
        secret.write_text('HINDSIGHT_API_KEY="synthetic-selected-key"\n')
        secret.chmod(0o600)
        output = tmp_path / "sdk-report"
        # Signature binding checks compatibility against the installed class.
        # Mutation spies fail even if future viewer changes bypass the transport.
        driver = '''
import inspect, runpy, sys
from hindsight_client import Hindsight
inspect.signature(Hindsight).bind(base_url="http://synthetic.invalid", api_key=None, timeout=30)
inspect.signature(Hindsight.recall).bind(None, bank_id="hermes-alpha", query="synthetic",
    types=["world", "experience"], tags=["session:requested"], tags_match="all_strict",
    include_entities=False, include_chunks=False, include_source_facts=False,
    trace=False, max_tokens=4096, budget="mid")
def forbidden(*args, **kwargs):
    raise AssertionError("PRIVATE forbidden SDK mutation")
for name in dir(Hindsight):
    if name.startswith(("retain", "aretain", "delete", "adelete", "import_", "aimport_",
                        "create", "acreate", "update", "aupdate", "reflect", "areflect")):
        setattr(Hindsight, name, forbidden)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
        proc = subprocess.run([python, "-c", driver, str(CLI), "--profile-home", str(profile),
                               "--session", "requested", "--reconstruct", "--output", str(output)],
                              env=env, capture_output=True, text=True, timeout=45)
        assert proc.returncode == (0 if scenario == "positive" else 3), proc.stderr
        assert proc.stdout == "private-report-written\n" and not proc.stderr
        assert len(requests) == 1  # no probes, retries, retain, import, delete, or bank setup
        method, path, body, auth = requests[0]
        assert auth == "Bearer synthetic-selected-key"
        assert (method, path) == ("POST", "/v1/default/banks/hermes-alpha/memories/recall")
        assert body["tags"] == ["session:requested"] and body["tags_match"] == "all_strict"
        assert body["types"] == ["world", "experience"]
        assert not any(body.get("include", {}).values()) and body["trace"] is False
        report = json.loads(output.read_text())
        assert report["messages"] == "missing" and report["session_record"] == "present"
        assert report["automatic_activity"] == "unknown: not inferred"
        fresh = report["reconstruction"]
        assert fresh["label"] == "reconstructed from current memory NOT original transcript"
        assert "PRIVATE" not in output.read_text() + proc.stdout + proc.stderr
        if scenario == "positive":
            assert fresh["status"] == "reconstructed"
            assert fresh["facts"][0]["text"] == "synthetic recovered fact"
        else:
            assert fresh["status"] == "unavailable" and fresh["facts"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("changes", [{"mode": "local_embedded"}, {"api_url": None},
                                     {"mode": "cloud"}, {"api_key": ["PRIVATE"]}])
def test_unavailable_connection_config_never_constructs_sdk(profile, tmp_path, sdk, changes):
    configure(profile, **changes)
    output = tmp_path / "config-error"
    assert invoke(profile, output, "--reconstruct") == 3
    assert json.loads(output.read_text())["reconstruction"]["facts"] == []
    sdk.factory.assert_not_called()


def test_incompatible_sdk_never_queries(profile, tmp_path, sdk):
    configure(profile)
    sdk.factory.recall = lambda self, bank_id, query: None
    output = tmp_path / "old-sdk"
    assert invoke(profile, output, "--reconstruct") == 3
    assert json.loads(output.read_text())["reconstruction"]["error"] == "reconstruction-sdk-incompatible"
    sdk.factory.assert_not_called()


def test_selected_profile_bank_and_provenance_are_bound(profile, tmp_path, monkeypatch, sdk):
    configure(profile)
    other = create_profile(tmp_path / "profiles" / "beta", name="beta")
    configure(other)
    first = tmp_path / "alpha-report"
    assert invoke(profile, first, "--reconstruct") == 0
    monkeypatch.setenv("HERMES_HOME", str(other))
    sdk.client.recall.return_value = SimpleNamespace(results=[fact(
        metadata={"session_id": "requested", "agent_identity": "beta"}, text="beta memory")])
    second = tmp_path / "beta-report"
    assert invoke(other, second, "--reconstruct") == 0
    assert [c.kwargs["bank_id"] for c in sdk.client.recall.call_args_list] == ["hermes-alpha", "hermes-beta"]
    assert "beta memory" not in first.read_text()
    assert "synthetic recovered fact" not in second.read_text()


@pytest.mark.parametrize("name", ["alpha--beta", "alpha.beta", "_alpha", "alpha__beta"])
def test_profile_sanitization_ambiguity_never_queries(tmp_path, monkeypatch, sdk, name):
    profile = create_profile(tmp_path / "profiles" / name, name=name)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    configure(profile)
    assert invoke(profile, tmp_path / "denied", "--reconstruct") == 3
    sdk.factory.assert_not_called()


def test_selected_profile_credentials_only_on_reconstruction(profile, tmp_path, monkeypatch, sdk):
    configure(profile, mode="cloud")
    monkeypatch.setenv("HINDSIGHT_API_KEY", "PRIVATE-ambient-key")
    secret = profile / ".env"
    secret.write_text('HINDSIGHT_API_KEY="synthetic-selected-key"\nUNRELATED=PRIVATE\n')
    secret.chmod(0o600)
    assert invoke(profile, tmp_path / "scoped-key", "--reconstruct") == 0
    assert sdk.factory.call_args.kwargs["api_key"] == "synthetic-selected-key"
    assert "PRIVATE" not in (tmp_path / "scoped-key").read_text()
    secret.unlink()
    sdk.factory.reset_mock()
    assert invoke(profile, tmp_path / "missing-key", "--reconstruct") == 3
    sdk.factory.assert_not_called()
