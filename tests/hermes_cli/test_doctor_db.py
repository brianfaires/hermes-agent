"""Synthetic-only coverage for doctor's bounded read-only database stage."""
import importlib.util
import sqlite3
import time
import json
import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import doctor_db as db


def test_limited_worker_reads_existing_database_without_writes(tmp_path):
    assert importlib.util.find_spec("hermes_cli.doctor_db") is not None
    from hermes_cli.doctor_db import probe_database

    path = tmp_path / "state #?雪.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE sessions(id); CREATE TABLE messages(id);")
    before = path.read_bytes()
    result = probe_database(path, "limited", time.monotonic() + 4)
    assert result["outcome"] == "pass"
    assert result["coverage"] == "limited"
    assert result["completed"] == ["sessions", "messages"]
    assert path.read_bytes() == before


def test_full_worker_completes_structural_check(tmp_path):
    from hermes_cli.doctor_db import probe_database
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE sessions(id); CREATE TABLE messages(id);")
    result = probe_database(path, "full", time.monotonic() + 4)
    assert result["coverage"] == "full_sqlite_structural"
    assert result["completed"] == ["integrity"]


def test_supervised_full_roundtrip_and_missing_file(tmp_path):
    from hermes_cli import doctor_db
    assert hasattr(doctor_db, "run_database_checks")
    path = tmp_path / "state.db"
    absent = doctor_db.run_database_checks(path)
    assert absent["outcome"] == "not_present"
    assert absent["cleanup"] == "confirmed"
    assert not path.exists()
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE sessions(id); CREATE TABLE messages(id);")
    result = doctor_db.run_database_checks(path, mode="full", timeout=5)
    assert result["coverage"] == "full_sqlite_structural"
    assert result["cleanup"] == "confirmed"
    assert doctor_db.database_exit_code(result) == 0


def test_cli_full_dispatch_and_validation(tmp_path, monkeypatch, capsys):
    import argparse
    from hermes_cli.subcommands.doctor import build_doctor_parser
    from hermes_cli.main import cmd_doctor
    import hermes_cli.doctor as doctor
    parser = argparse.ArgumentParser()
    build_doctor_parser(parser.add_subparsers(), cmd_doctor=cmd_doctor)
    args = parser.parse_args(["doctor", "--db-integrity", "full", "--db-timeout", "2"])
    monkeypatch.setattr(doctor, "HERMES_HOME", tmp_path)
    assert args.func(args) == 3
    assert "not present" in capsys.readouterr().out


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE sessions(id); CREATE TABLE messages(id);"
                           "INSERT INTO sessions VALUES (1); INSERT INTO messages VALUES (1);")
    return path


def test_real_progress_interrupt_has_no_corruption_or_next_statement():
    now = [0.]
    budget = db.Deadline(1., clock=lambda: now[0])
    conn = sqlite3.connect(":memory:")
    callbacks = []
    def progress():
        callbacks.append(1)
        if len(callbacks) == 5:
            now[0] = 1.
        return budget.progress()
    conn.set_progress_handler(progress, 1000)
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            list(budget.rows(conn, "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000000) SELECT sum(x) FROM n"))
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
        assert db._classify(caught.value, budget) == ("incomplete", "deadline")
        with pytest.raises(db.ProbeStopped):
            list(budget.rows(conn, "SELECT 1"))
    finally:
        conn.close()


@pytest.mark.parametrize("remaining,expected", [(2., 100), (.05, 50), (.0001, 0)])
def test_busy_allowance_is_remaining_budget(remaining, expected):
    conn = sqlite3.connect(":memory:")
    trace = []
    conn.set_trace_callback(trace.append)
    try:
        assert list(db.Deadline(remaining, clock=lambda: 0).rows(conn, "SELECT 1")) == [(1,)]
        assert trace[0] == f"PRAGMA busy_timeout={expected}"
    finally:
        conn.close()


def test_real_exclusive_lock_incomplete_then_released(database):
    conn = sqlite3.connect(database)
    conn.execute("BEGIN EXCLUSIVE")
    try:
        result = db.run_database_checks(database)
        assert result["reason"] in ("busy", "locked")
        assert db.database_exit_code(result) == 3
    finally:
        conn.rollback()
        conn.close()
    writer = sqlite3.connect(database, timeout=.5)
    try:
        writer.execute("BEGIN EXCLUSIVE")
    finally:
        writer.close()


def test_wal_live_writer_and_profile_path_isolation(database, tmp_path):
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO messages VALUES (2)")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = db.run_database_checks(database)
        assert result["outcome"] == "pass"
        assert result["metadata"]["journal_mode"] == "wal"
        assert db.run_database_checks(tmp_path / "other" / "state.db")["outcome"] == "not_present"
    finally:
        conn.rollback()
        conn.close()


def test_stale_index_detected_only_by_full_structural_check(database):
    original = "CREATE INDEX stale_idx ON messages(id)"
    def set_definition(conn, sql):
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("UPDATE sqlite_master SET sql=? WHERE name='stale_idx'", (sql,))
        version = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute(f"PRAGMA schema_version={version + 1}")
        conn.execute("PRAGMA writable_schema=OFF")
        conn.commit()
    with sqlite3.connect(database) as conn:
        conn.execute(original)
        set_definition(conn, original + " WHERE 0")
    with sqlite3.connect(database) as conn:
        conn.execute("REINDEX stale_idx")
        set_definition(conn, original)
    before = database.read_bytes()
    assert db.run_database_checks(database)["coverage"] == "limited"
    result = db.run_database_checks(database, "full", 5)
    assert db.database_exit_code(result) == 1
    assert result["findings"] == ["integrity"]
    assert result["coverage"] == "incomplete"
    assert database.read_bytes() == before


def test_real_fts_read_damage(database):
    with sqlite3.connect(database) as conn:
        conn.executescript("CREATE VIRTUAL TABLE messages_fts USING fts5(content);"
                           "INSERT INTO messages_fts VALUES ('synthetic only');"
                           "UPDATE messages_fts_data SET block=X'BADC0FFEE0DDF00D';")
    result = db.run_database_checks(database)
    assert db.database_exit_code(result) == 1
    assert result["findings"] == ["messages_fts"]
    assert "synthetic" not in json.dumps(result)


@pytest.mark.parametrize("code,outcome,reason", [
    (sqlite3.SQLITE_CORRUPT | 256, "corruption", "sqlite"),
    (sqlite3.SQLITE_BUSY, "incomplete", "busy"),
    (sqlite3.SQLITE_LOCKED, "incomplete", "locked"),
    (sqlite3.SQLITE_NOTADB, "error", "not_database"),
    (sqlite3.SQLITE_IOERR, "error", "sqlite"),
    (sqlite3.SQLITE_NOMEM, "error", "sqlite"),
    (sqlite3.SQLITE_INTERRUPT, "error", "sqlite"),
])
def test_classification_uses_codes_not_scary_text(code, outcome, reason):
    exc = sqlite3.OperationalError("corrupt secret-value")
    exc.sqlite_errorcode = code
    assert db._classify(exc, db.Deadline(time.monotonic() + 1)) == (outcome, reason)


def test_missing_schema_and_notadb_not_healthy(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated(id)")
    assert db.run_database_checks(path, "full", 5)["reason"] == "schema"
    path.write_bytes(b"synthetic not sqlite" * 200)
    result = db.run_database_checks(path)
    assert db.database_exit_code(result) == 4
    assert result["reason"] == "not_database"


def test_read_only_sql_trace_and_close(database, monkeypatch):
    connect = sqlite3.connect
    trace, closed = [], []
    class Connection(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()
    def open_connection(*args, **kwargs):
        assert kwargs["uri"] is True and args[0].endswith("?mode=ro")
        conn = connect(*args, **kwargs, factory=Connection)
        conn.set_trace_callback(trace.append)
        return conn
    monkeypatch.setattr(db.sqlite3, "connect", open_connection)
    assert db.probe_database(database, "limited", time.monotonic() + 2)["outcome"] == "pass"
    sql = "\n".join(trace).lower()
    for forbidden in ("count(", "integrity_check", "quick_check", "insert", "update", "checkpoint", "begin", "reindex"):
        assert forbidden not in sql
    assert closed == [True]


def test_cleanup_schedule_and_unconfirmed_exit():
    now, calls = [0.], []
    class Process:
        def poll(self):
            return None
        def terminate(self):
            calls.append(("terminate", now[0]))
        def kill(self):
            calls.append(("kill", now[0]))
    def sleep(seconds):
        now[0] += seconds
    status, action = db._finish_child(Process(), 1., lambda: 0, clock=lambda: now[0], sleep=sleep)
    assert status == "unconfirmed" and action == "kill"
    assert calls == [("terminate", .25), ("kill", .5)]
    assert now[0] == 1.


@pytest.mark.linux_only
def test_real_forced_kill_no_survivor(database, tmp_path, monkeypatch):
    worker = tmp_path / "stalled.py"
    worker.write_text("import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                      "print('{\"v\":1,\"type\":\"stage\",\"stage\":\"open\"}',flush=True)\n"
                      "time.sleep(60)\n")
    real_popen = subprocess.Popen
    children = []
    def launch(argv, **kwargs):
        child = real_popen([sys.executable, "-I", str(worker)], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(db.subprocess, "Popen", launch)
    start = time.monotonic()
    result = db.run_database_checks(database, timeout=1.5)
    assert time.monotonic() - start < 2.5
    assert result["reason"] == "deadline"
    assert result["termination"] == "kill"
    assert result["cleanup"] == "confirmed"
    assert children[0].poll() == -signal.SIGKILL


def _wire(record):
    return json.dumps({"v": 1, **record}).encode() + b"\n"


def _fake_supervisor(monkeypatch, chunks, *, returncode=0, read_clock=None):
    stream = SimpleNamespace(fileno=lambda: 123, close=lambda: None)
    process = SimpleNamespace(stdout=stream, pid=123, returncode=returncode, poll=lambda: returncode)
    monkeypatch.setattr(db.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(db.os, "set_blocking", lambda *a: None)
    items = iter(chunks)
    def read(pipe):
        if read_clock:
            read_clock()
        return next(items, b"")
    monkeypatch.setattr(db, "_read_available", read)
    return process


@pytest.mark.parametrize("payload", [b"", b"{", b"{}\n", b"x" * (db.MAX_OUTPUT + 1),
                                     _wire({"type": "stage", "stage": "private-row"})])
def test_malformed_stream_never_passes(monkeypatch, tmp_path, payload):
    _fake_supervisor(monkeypatch, [payload])
    result = db.run_database_checks(tmp_path / "absent")
    assert db.database_exit_code(result) == 4


def test_received_corruption_survives_truncated_result(monkeypatch, tmp_path):
    _fake_supervisor(monkeypatch, [_wire({"type": "finding", "stage": "integrity"}), b"{"])
    result = db.run_database_checks(tmp_path / "absent", "full", 5)
    assert db.database_exit_code(result) == 1
    assert result["reason"] == "protocol"
    assert result["coverage"] == "incomplete"


def test_terminal_before_deadline_child_exit_during_cleanup(monkeypatch, tmp_path):
    value = db._result("limited")
    value.update(outcome="pass", coverage="limited", completed=["sessions", "messages"])
    now = [0.]
    monkeypatch.setattr(db, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s)))
    _fake_supervisor(monkeypatch, [_wire({"type": "result", "result": value}), None],
                     read_clock=lambda: now.__setitem__(0, now[0] + 2.1))
    result = db.run_database_checks(tmp_path / "absent")
    assert db.database_exit_code(result) == 0


@pytest.mark.parametrize("flags", [
    ["--db-timeout", "4"], ["--db-integrity", "full", "--fix"],
    ["--db-integrity", "full", "--live"], ["--db-integrity", "full", "--ack", "anything"],
    ["--db-strict", "--fix"], ["--db-strict", "--ack", "anything"],
    *[["--db-integrity", "full", "--db-timeout", v] for v in ("0", "-1", "nan", "inf", "1.5", "86401")],
])
def test_invalid_flags_before_worker(flags, monkeypatch):
    import argparse
    from hermes_cli.subcommands.doctor import build_doctor_parser
    from hermes_cli.main import cmd_doctor
    monkeypatch.setattr(db, "run_database_checks", lambda *a, **k: pytest.fail("worker invoked"))
    parser = argparse.ArgumentParser()
    build_doctor_parser(parser.add_subparsers(), cmd_doctor=cmd_doctor)
    with pytest.raises(SystemExit) as caught:
        args = parser.parse_args(["doctor", *flags])
        args.func(args)
    assert caught.value.code == 2


@pytest.mark.parametrize("outcome,reason,findings,cleanup,expected", [
    ("pass", "none", [], "confirmed", 0), ("pass", "none", [], "unconfirmed", 4),
    ("incomplete", "deadline", ["integrity"], "unconfirmed", 1),
    ("incomplete", "cancelled", ["integrity"], "unconfirmed", 130),
    ("incomplete", "busy", [], "confirmed", 3), ("error", "io", [], "confirmed", 4),
])
def test_exit_precedence(outcome, reason, findings, cleanup, expected):
    result = db._result("limited")
    result.update(outcome=outcome, reason=reason, findings=findings, cleanup=cleanup)
    assert db.database_exit_code(result) == expected


def test_completed_subprobes_survive_missing_terminal(monkeypatch, tmp_path):
    event = dict(type="stage", stage="messages", completed=["sessions"], skipped={})
    _fake_supervisor(monkeypatch, [_wire(event), b"{"])
    result = db.run_database_checks(tmp_path / "absent")
    assert result["completed"] == ["sessions"]
    assert db.database_exit_code(result) == 4


def test_corruption_in_optional_metadata_not_discarded(database, monkeypatch):
    original = db.Deadline.rows
    def rows(self, conn, sql, parameters=()):
        if sql == "PRAGMA page_count":
            exc = sqlite3.DatabaseError("do not emit this")
            exc.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
            raise exc
        yield from original(self, conn, sql, parameters)
    monkeypatch.setattr(db.Deadline, "rows", rows)
    result = db.probe_database(database, "limited", time.monotonic() + 4)
    assert result["findings"] == ["metadata"]


def test_capability_is_explicit_per_fts_probe(database, monkeypatch):
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content)")
    original = db.Deadline.rows
    def rows(self, conn, sql, parameters=()):
        if "MATCH" in sql:
            raise sqlite3.OperationalError("no such module: fts5")
        yield from original(self, conn, sql, parameters)
    monkeypatch.setattr(db.Deadline, "rows", rows)
    result = db.probe_database(database, "limited", time.monotonic() + 4)
    assert result["skipped"] == {"messages_fts": "capability", "messages_fts_trigram": "absent", "messages_fts_cjk": "absent"}
    assert result["outcome"] == "incomplete"


@pytest.mark.parametrize("when", ["launch", "read", "cleanup"])
def test_ctrl_c_cleans_child_and_returns_130(monkeypatch, tmp_path, when):
    child = _fake_supervisor(monkeypatch, [])
    def interrupt(*args, **kwargs):
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        return child
    if when == "launch":
        monkeypatch.setattr(db.subprocess, "Popen", interrupt)
    elif when == "read":
        monkeypatch.setattr(db, "_read_available", lambda pipe: (interrupt(), b"")[1])
    else:
        monkeypatch.setattr(db, "_finish_child", lambda *a, **k: (interrupt(), ("confirmed", "none"))[1])
    before = signal.getsignal(signal.SIGINT)
    result = db.run_database_checks(tmp_path / "absent")
    assert db.database_exit_code(result) == 130
    assert signal.getsignal(signal.SIGINT) == before


def test_late_success_is_incomplete(monkeypatch, tmp_path):
    value = db._result("limited")
    value.update(outcome="pass", coverage="limited", completed=["sessions", "messages"])
    now = [0.]
    monkeypatch.setattr(db, "time", SimpleNamespace(monotonic=lambda: now[0]))
    _fake_supervisor(monkeypatch, [_wire({"type": "result", "result": value})],
                     read_clock=lambda: now.__setitem__(0, 4.))
    result = db.run_database_checks(tmp_path / "absent")
    assert db.database_exit_code(result) == 3
    assert result["reason"] == "deadline"


def test_startup_exhausts_same_budget(monkeypatch, tmp_path):
    now = [0.]
    monkeypatch.setattr(db, "time", SimpleNamespace(monotonic=lambda: now[0]))
    child = _fake_supervisor(monkeypatch, [])
    def launch(*args, **kwargs):
        assert float(args[0][-1]) == 4.
        now[0] = 4.
        return child
    monkeypatch.setattr(db.subprocess, "Popen", launch)
    result = db.run_database_checks(tmp_path / "absent")
    assert result["reason"] == "deadline"


@pytest.mark.parametrize("stage", ["connect", "setup", "fetch", "metadata", "emit"])
def test_worker_closes_on_exception(database, monkeypatch, stage):
    connect = sqlite3.connect
    closed, cursors = [], []
    class Cursor(sqlite3.Cursor):
        def fetchone(self):
            if stage == "fetch":
                raise sqlite3.OperationalError("synthetic fetch failure")
            return super().fetchone()
        def close(self):
            cursors.append(self)
            super().close()
    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if stage == "setup" and sql == "PRAGMA query_only=ON":
                raise sqlite3.OperationalError("synthetic setup failure")
            if stage == "metadata" and sql == "PRAGMA page_count":
                raise sqlite3.OperationalError("synthetic metadata failure")
            return self.cursor(factory=Cursor).execute(sql, parameters)
        def close(self):
            closed.append(self)
            super().close()
    def launch(*args, **kwargs):
        if stage == "connect":
            raise OSError("synthetic connect failure")
        return connect(*args, **kwargs, factory=Connection)
    def emit(record):
        if stage == "emit" and record.get("stage") == "messages":
            raise OSError("synthetic serialization failure")
    monkeypatch.setattr(db.sqlite3, "connect", launch)
    db.probe_database(database, "limited", time.monotonic() + 4, emit)
    assert len(closed) == (0 if stage == "connect" else 1)
    assert len(cursors) == len(set(map(id, cursors)))


@pytest.mark.parametrize("reason,cleanup,expected", [
    ("deadline", "confirmed", 3), ("busy", "confirmed", 3),
    ("protocol", "confirmed", 4), ("deadline", "unconfirmed", 4),
    ("cancelled", "confirmed", 130),
])
def test_ordinary_continues_without_parent_db_fallback(tmp_path, monkeypatch, capsys, reason, cleanup, expected):
    from argparse import Namespace
    import hermes_cli.doctor as doctor
    import hermes_state
    result = db._result("limited")
    result.update(reason=reason, outcome="error" if reason == "protocol" else "incomplete",
                  cleanup=cleanup, termination="none", pid=123, elapsed_seconds=4.)
    monkeypatch.setattr(db, "run_database_checks", lambda *a, **k: result)
    monkeypatch.setattr(doctor, "HERMES_HOME", tmp_path)
    monkeypatch.setattr(doctor, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""))
    for name in ("_db_opens_cleanly", "collect_state_db_stats", "count_db_holders", "repair_state_db_schema"):
        monkeypatch.setattr(hermes_state, name, lambda *a, **k: pytest.fail("legacy DB fallback"))
    calls = []
    monkeypatch.setattr(doctor, "_check_gateway_service_linger", lambda issues: calls.append("next"))
    assert doctor.run_doctor(Namespace(fix=False, db_strict=True)) == expected
    assert calls == ([] if expected == 130 else ["next"])
    output = capsys.readouterr().out
    assert "All checks passed" not in output
    assert "NOT checked" in output
    if expected != 130:
        calls.clear()
        assert doctor.run_doctor(Namespace(fix=False)) is None
        assert calls == ["next"]


def test_real_cli_full_exit(database, monkeypatch):
    env = dict(os.environ, HOME=str(database.parent), HERMES_HOME=str(database.parent))
    result = subprocess.run([sys.executable, "-m", "hermes_cli.main", "doctor", "--db-integrity", "full", "--db-timeout", "5"],
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "Full SQLite structural integrity check completed: ok" in result.stdout
    assert "FTS write health" in result.stdout


@pytest.mark.windows_only
def test_windows_pipe_roundtrip(database):
    assert db.database_exit_code(db.run_database_checks(database, "full", 5)) == 0


@pytest.mark.macos_only
def test_macos_pipe_roundtrip(database):
    assert db.database_exit_code(db.run_database_checks(database, "full", 5)) == 0


@pytest.mark.parametrize("contradiction", ["capability", "lost_progress", "lost_finding", "lost_skip", "changed_skip"])
def test_contradictory_terminal_never_passes(monkeypatch, tmp_path, contradiction):
    value = db._result("limited")
    value.update(outcome="pass", coverage="limited", completed=["sessions", "messages"])
    chunks = []
    if contradiction == "capability":
        value["skipped"] = {"messages_fts": "capability"}
    elif contradiction == "lost_progress":
        chunks.append(_wire(dict(type="stage", stage="metadata",
                                 completed=["sessions", "messages", "messages_fts"], skipped={})))
    elif contradiction in ("lost_skip", "changed_skip"):
        chunks.append(_wire(dict(type="stage", stage="messages_fts_trigram",
                                 completed=["sessions", "messages"], skipped={"messages_fts": "capability"})))
        value["skipped"] = {} if contradiction == "lost_skip" else {"messages_fts": "absent"}
        value["last_stage"] = "metadata"
        chunks.append(_wire(dict(type="stage", stage="metadata",
                                 completed=["sessions", "messages"], skipped=value["skipped"])))
    else:
        chunks.append(_wire(dict(type="finding", stage="messages_fts")))
    chunks.append(_wire(dict(type="result", result=value)))
    _fake_supervisor(monkeypatch, chunks)
    result = db.run_database_checks(tmp_path / "absent")
    assert db.database_exit_code(result) == (1 if contradiction == "lost_finding" else 4)
    assert result["reason"] == "protocol"


@pytest.mark.parametrize("outcome,reason,findings,expected", [
    ("incomplete", "capability", [], 4), ("not_present", "none", [], 4),
    ("corruption", "sqlite", ["integrity"], 1),
])
def test_unexpected_exit_after_terminal_retained(monkeypatch, tmp_path, outcome, reason, findings, expected):
    value = db._result("full")
    value.update(outcome=outcome, reason=reason, findings=findings)
    _fake_supervisor(monkeypatch, [_wire(dict(type="result", result=value))], returncode=2)
    result = db.run_database_checks(tmp_path / "absent", "full", 5)
    assert db.database_exit_code(result) == expected
    assert result["reason"] == "child_exit"


@pytest.mark.linux_only
@pytest.mark.parametrize("repeat", [False, True])
def test_live_sql_cancel_releases_reader_lock(database, tmp_path, monkeypatch, repeat):
    # A synthetic read transaction, not a sleeper: SIGINT is handled during
    # actual SQLite VM execution, and the parent proves the read lock released.
    worker = tmp_path / "reader.py"
    worker.write_text(
        "import sqlite3,signal,sys,time\n"
        "cancelled=False\n"
        "def cancel(s,f):\n global cancelled\n cancelled=True\n"
        "signal.signal(signal.SIGINT,cancel)\n"
        "conn=sqlite3.connect(sys.argv[1]+'?mode=ro',uri=True)\n"
        "conn.set_progress_handler(lambda:int(cancelled),1000)\n"
        "conn.execute('BEGIN')\n"
        "conn.execute('SELECT 1 FROM messages').fetchall()\n"
        "print('{\"v\":1,\"type\":\"stage\",\"stage\":\"open\"}',flush=True)\n"
        "try:\n conn.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n').fetchone()\n"
        "except sqlite3.OperationalError:\n pass\n"
        "finally:\n conn.close()\n"
    )
    popen = subprocess.Popen
    children = []
    def launch(argv, **kwargs):
        child = popen([sys.executable, "-I", str(worker), database.as_uri()], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(db.subprocess, "Popen", launch)
    read = db._read_available
    ready = []
    def receive(pipe):
        data = read(pipe)
        if data and not ready:
            ready.append(True)
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)
            if repeat:
                handler(signal.SIGINT, None)
        return data
    monkeypatch.setattr(db, "_read_available", receive)
    before = database.read_bytes()
    result = db.run_database_checks(database)
    assert ready and db.database_exit_code(result) == 130
    assert result["cleanup"] == "confirmed"
    assert children[0].poll() is not None
    writer = sqlite3.connect(database, timeout=.5)
    try:
        writer.execute("BEGIN EXCLUSIVE")
    finally:
        writer.close()
    assert database.read_bytes() == before
