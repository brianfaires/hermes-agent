"""Read-only doctor probes. No SessionDB initialization or repair belongs here.

Only the supervised child may touch database paths. The synchronous probe is
also used by synthetic tests; callers must use run_database_checks instead.
"""
from __future__ import annotations

import json
import math
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

FTS_TABLES = ("messages_fts", "messages_fts_trigram", "messages_fts_cjk")
PROBES = ("sessions", "messages", *FTS_TABLES, "integrity")
STAGES = ("starting", "open", "setup", "schema", *PROBES, "metadata", "close", "result")
REASONS = ("none", "deadline", "cancelled", "busy", "locked", "capability",
           "schema", "sqlite", "not_database", "io", "protocol", "child_exit", "cleanup")


class ProbeStopped(Exception):
    def __init__(self, reason):
        self.reason = reason


class Deadline:
    def __init__(self, deadline, cancelled=lambda: False, clock=time.monotonic):
        self.deadline = deadline
        self.cancelled = cancelled
        self.clock = clock

    def check(self):
        if self.cancelled():
            raise ProbeStopped("cancelled")
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise ProbeStopped("deadline")
        return remaining

    def progress(self):
        return int(self.cancelled() or self.clock() >= self.deadline)

    def rows(self, conn, sql, parameters=()):
        """Single budget, including prepare, step, fetch and cursor cleanup."""
        remaining = self.check()
        cursor = None
        try:
            cursor = conn.execute(f"PRAGMA busy_timeout={min(100, int(remaining * 1000))}")
            cursor.close()
            cursor = None
            self.check()
            cursor = conn.execute(sql, parameters)
            self.check()
            while True:
                self.check()
                row = cursor.fetchone()
                self.check()
                if row is None:
                    break
                yield row
        finally:
            if cursor is not None:
                cursor.close()


def _result(mode) -> dict:
    return dict(mode=mode, outcome="incomplete", coverage="incomplete", reason="none",
                completed=[], skipped={}, findings=[], metadata={}, last_stage="starting")


def _classify(exc, budget):
    if isinstance(exc, ProbeStopped):
        return "incomplete", exc.reason
    code = getattr(exc, "sqlite_errorcode", 0) & 255
    if code == sqlite3.SQLITE_CORRUPT:
        return "corruption", "sqlite"
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return "incomplete", "busy" if code == sqlite3.SQLITE_BUSY else "locked"
    if code == sqlite3.SQLITE_INTERRUPT:
        try:
            budget.check()
        except ProbeStopped as stopped:
            return "incomplete", stopped.reason
    # SQLite does not give missing tokenizers their own numeric error code.
    # Use only these fixed capability messages, never general corruption text.
    if isinstance(exc, sqlite3.OperationalError) and any(
        item in str(exc).lower() for item in (
            "no such module: fts5", "no such tokenizer: trigram",
            "no such tokenizer: cjk_unicode61",
        )
    ):
        return "incomplete", "capability"
    if code == sqlite3.SQLITE_NOTADB:
        return "error", "not_database"
    return "error", "sqlite" if isinstance(exc, sqlite3.Error) else "io"


def probe_database(path, mode, deadline, emit=lambda record: None, *,
                   cancelled=lambda: False, clock=time.monotonic):
    """Run fixed SQL only; emit sanitized evidence, never exception/row text."""
    result = _result(mode)
    budget = Deadline(deadline, cancelled, clock)
    conn = None

    def stage(name):
        budget.check()
        result["last_stage"] = name
        emit({"type": "stage", "stage": name,
              "completed": list(result["completed"]), "skipped": dict(result["skipped"])})

    def finding(name):
        if name not in result["findings"] and len(result["findings"]) < 3:
            result["findings"].append(name)
            emit({"type": "finding", "stage": name})

    def rows(sql, parameters=()):
        return list(budget.rows(conn, sql, parameters))

    try:
        stage("open")
        path = Path(path)
        if not path.exists():
            result.update(outcome="not_present", coverage="not_present")
            return result
        conn = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True,
                               timeout=min(.100, budget.check()))
        conn.set_progress_handler(budget.progress, 1000)
        stage("setup")
        rows("PRAGMA query_only=ON")
        result["metadata"]["journal_mode"] = rows("PRAGMA journal_mode")[0][0]
        stage("schema")
        for table in ("sessions", "messages"):
            if not rows("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=? LIMIT 1", (table,)):
                result.update(reason="schema")
                return result
        if mode == "full":
            stage("integrity")
            count = 0
            for row in budget.rows(conn, "PRAGMA main.integrity_check"):
                count += 1
                if row != ("ok",):
                    finding("integrity")
            if count == 1 and not result["findings"]:
                result.update(outcome="pass", coverage="full_sqlite_structural")
                result["completed"].append("integrity")
            elif result["findings"]:
                result.update(outcome="corruption", reason="sqlite")
            else:
                result.update(outcome="error", reason="sqlite")
            return result
        for table in ("sessions", "messages"):
            stage(table)
            rows(f"SELECT 1 FROM {table} LIMIT 1")
            result["completed"].append(table)
        for table in FTS_TABLES:
            if not rows("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=? LIMIT 1", (table,)):
                result["skipped"][table] = "absent"
                continue
            stage(table)
            try:
                # The installed custom tokenizer is deliberately not loaded:
                # no arbitrary native extension or profile initialization here.
                # Its absence is a visible capability gap, never a healthy pass.
                rows(f"SELECT 1 FROM {table} WHERE {table} MATCH '\"\"' LIMIT 1")
                result["completed"].append(table)
            except sqlite3.Error as exc:
                outcome, reason = _classify(exc, budget)
                if reason != "capability":
                    raise
                result["skipped"][table] = "capability"
                result.update(reason=reason)
        if result["reason"] == "none":
            result.update(outcome="pass", coverage="limited")
        stage("metadata")
        try:
            for name in ("page_count", "page_size", "freelist_count"):
                result["metadata"][name] = rows(f"PRAGMA {name}")[0][0]
            budget.check()
            result["metadata"]["file_bytes"] = path.stat().st_size
            budget.check()
            wal = Path(str(path) + "-wal")
            result["metadata"]["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
            budget.check()
        except sqlite3.Error as exc:
            outcome, reason = _classify(exc, budget)
            if outcome == "corruption" or reason in ("deadline", "cancelled"):
                raise
            result["metadata"]["unavailable"] = True
        except OSError:
            result["metadata"]["unavailable"] = True
    except Exception as exc:
        outcome, reason = _classify(exc, budget)
        if outcome == "corruption":
            finding(result["last_stage"])
        result.update(outcome=outcome, coverage="incomplete", reason=reason)
    finally:
        if conn is not None:
            try:
                conn.set_progress_handler(None, 0)
                conn.close()
            except Exception:
                result.update(outcome="error", coverage="incomplete", reason="cleanup")
    return result


MAX_OUTPUT = 16 * 1024


def _names(value, vocabulary, maximum):
    return (isinstance(value, list) and len(value) <= maximum
            and all(isinstance(v, str) and v in vocabulary for v in value)
            and len(set(value)) == len(value))


def _valid_skips(value):
    return (isinstance(value, dict) and len(value) <= 3
            and all(k in FTS_TABLES and v in ("absent", "capability") for k, v in value.items()))


def _valid_result(value, mode):
    if not isinstance(value, dict) or set(value) != set(_result(mode)):
        return False
    if (value["mode"] != mode
            or value["outcome"] not in ("pass", "corruption", "incomplete", "error", "not_present")
            or value["coverage"] not in ("limited", "full_sqlite_structural", "incomplete", "not_present")
            or value["reason"] not in REASONS or value["last_stage"] not in STAGES
            or not _names(value["completed"], PROBES, len(PROBES))
            or not _valid_skips(value["skipped"])
            or not _names(value["findings"], STAGES, 3)):
        return False
    metadata = value["metadata"]
    if not isinstance(metadata, dict):
        return False
    for key, item in metadata.items():
        if key == "journal_mode":
            if item not in ("delete", "truncate", "persist", "memory", "wal", "off"):
                return False
        elif key == "unavailable":
            if item is not True:
                return False
        elif key not in ("page_count", "page_size", "freelist_count", "file_bytes", "wal_bytes") or type(item) is not int or not 0 <= item <= 2**63 - 1:
            return False
    if set(value["completed"]) & set(value["skipped"]):
        return False
    if value["outcome"] == "pass":
        required = {"integrity"} if mode == "full" else {"sessions", "messages"}
        coverage = "full_sqlite_structural" if mode == "full" else "limited"
        return (required <= set(value["completed"]) and value["coverage"] == coverage
                and value["reason"] == "none" and not value["findings"]
                and "capability" not in value["skipped"].values())
    return value["coverage"] in ("incomplete", "not_present")


class _Records:
    """Finite internal JSONL protocol; never wait for a newline or EOF."""
    def __init__(self, mode):
        self.mode = mode
        self.buffer = b""
        self.total = 0
        self.stage_index = -1
        self.findings = []
        self.completed = []
        self.skipped = {}
        self.terminal = None

    def feed(self, data):
        self.total += len(data)
        if self.total > MAX_OUTPUT:
            raise ValueError("oversized protocol")
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            record = json.loads(line)
            if not isinstance(record, dict) or record.get("v") != 1 or self.terminal is not None:
                raise ValueError("invalid protocol")
            kind = record.get("type")
            if kind in ("stage", "finding") and set(record) in (
                {"v", "type", "stage"}, {"v", "type", "stage", "completed", "skipped"}
            ):
                stage = record["stage"]
                if stage not in STAGES:
                    raise ValueError("invalid stage")
                if kind == "stage":
                    index = STAGES.index(stage)
                    if index <= self.stage_index:
                        raise ValueError("stage order")
                    completed = record.get("completed", [])
                    skipped = record.get("skipped", {})
                    if (not _names(completed, PROBES, len(PROBES))
                            or not _valid_skips(skipped)
                            or not set(self.completed) <= set(completed)
                            or any(skipped.get(k) != v for k, v in self.skipped.items())):
                        raise ValueError("invalid progress")
                    self.completed, self.skipped = completed, skipped
                    self.stage_index = index
                else:
                    if len(self.findings) >= 3 or stage in self.findings:
                        raise ValueError("finding limit")
                    self.findings.append(stage)
            elif (kind == "result" and set(record) == {"v", "type", "result"}
                  and _valid_result(record["result"], self.mode)):
                terminal = record["result"]
                if (not set(self.completed) <= set(terminal["completed"])
                        or not set(self.findings) <= set(terminal["findings"])
                        or any(terminal["skipped"].get(k) != v for k, v in self.skipped.items())
                        or (self.stage_index >= 0 and terminal["last_stage"] != STAGES[self.stage_index])):
                    raise ValueError("contradictory terminal")
                self.terminal = terminal
            else:
                raise ValueError("invalid record")


def _read_available(pipe):
    if os.name == "nt":
        # Peek avoids a blocking read, including Python <3.12 on Windows.
        import ctypes
        import msvcrt
        from ctypes import wintypes
        available = wintypes.DWORD()
        peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
        peek.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                         wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
        peek.restype = wintypes.BOOL
        if not peek(msvcrt.get_osfhandle(pipe.fileno()), None, 0, None,
                    ctypes.byref(available), None):
            if ctypes.get_last_error() == 109:
                return b""
            raise OSError("pipe read failed")
        if not available.value:
            return None
        return os.read(pipe.fileno(), min(4096, available.value))
    try:
        return os.read(pipe.fileno(), 4096)
    except BlockingIOError:
        return None


def _finish_child(proc, deadline, cancelled, *, clock=time.monotonic, sleep=time.sleep):
    """Finite grace/terminate/kill. No communicate, join or blocking reap."""
    action = "none"
    start = clock()
    if proc.poll() is not None:
        return "confirmed", action
    if cancelled() and os.name != "nt":
        try:
            proc.send_signal(signal.SIGINT)
            action = "interrupt"
        except OSError:
            pass
    for threshold, operation in ((.25, "terminate"), (.50, "kill"), (1., None)):
        end = min(deadline, start + threshold)
        while proc.poll() is None and clock() < end:
            if cancelled() > 1 and operation is not None:
                break
            try:
                sleep(min(.01, max(0, end - clock())))
            except KeyboardInterrupt:
                break
        if proc.poll() is not None:
            return "confirmed", action
        if operation:
            try:
                getattr(proc, operation)()
                action = operation
            except OSError:
                pass
    return ("confirmed" if proc.poll() is not None else "unconfirmed"), action


def run_database_checks(path, mode="limited", timeout=None, *, started=None):
    """Bound the complete DB stage, including startup and cleanup.

    The caller records started at stage entry before importing this module.
    Userspace cannot guarantee resource release during kernel/I/O stalls.
    """
    start = time.monotonic() if started is None else started
    total = (600 if mode == "full" else 5) if timeout is None else timeout
    if mode not in ("limited", "full") or not math.isfinite(total) or not 1 < total <= 86400:
        raise ValueError("invalid database budget/mode")
    deadline, shutdown = start + total - 1, start + total
    result = _result(mode)
    records = _Records(mode)
    proc = None
    cancellation = 0
    old_handler = None

    def cancel(signum, frame):
        nonlocal cancellation
        cancellation += 1

    try:
        if threading.current_thread() is threading.main_thread():
            old_handler = signal.signal(signal.SIGINT, cancel)
        if time.monotonic() >= deadline:
            raise ProbeStopped("deadline")
        # Fixed module file, isolated startup; no CLI/plugin/config imports or
        # inherited credentials. The selected path is the only profile input.
        env = {key: os.environ[key] for key in
               ("SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "TZ") if key in os.environ}
        proc = subprocess.Popen(
            [sys.executable, "-I", __file__, str(Path(path).absolute()), mode, str(deadline)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, start_new_session=os.name != "nt", bufsize=0,
        )
        assert proc.stdout is not None
        if os.name != "nt":
            os.set_blocking(proc.stdout.fileno(), False)
        while True:
            if cancellation:
                raise ProbeStopped("cancelled")
            if time.monotonic() >= deadline:
                raise ProbeStopped("deadline")
            data = _read_available(proc.stdout)
            if data:
                records.feed(data)
                if time.monotonic() >= deadline:
                    raise ProbeStopped("deadline")
                if records.terminal is not None:
                    result = records.terminal
                    if records.buffer:
                        raise ValueError("trailing partial record")
                    break
            elif data == b"":
                if records.buffer or records.terminal is None:
                    raise ValueError("truncated protocol")
                break
            else:
                time.sleep(min(.01, max(0, deadline - time.monotonic())))
    except (ProbeStopped, KeyboardInterrupt) as exc:
        reason = exc.reason if isinstance(exc, ProbeStopped) else "cancelled"
        if isinstance(exc, KeyboardInterrupt):
            cancellation += 1
        result.update(outcome="incomplete", coverage="incomplete",
                      reason="cancelled" if cancellation else reason)
    except Exception:
        result.update(outcome="error", coverage="incomplete", reason="protocol")
    finally:
        cleanup, action = "confirmed", "none"
        if proc is not None:
            try:
                cleanup, action = _finish_child(
                    proc, min(shutdown, time.monotonic() + 1), lambda: cancellation)
                if proc.returncode not in (None, 0) and action == "none":
                    result.update(outcome="error", coverage="incomplete", reason="child_exit")
                if records.terminal is not None and cleanup == "confirmed":
                    if _read_available(proc.stdout):
                        result.update(outcome="error", coverage="incomplete", reason="protocol")
            except Exception:
                cleanup = "unconfirmed"
                result.update(outcome="error", coverage="incomplete", reason="cleanup")
            finally:
                if proc.stdout is not None:
                    try:
                        proc.stdout.close()
                    except OSError:
                        cleanup = "unconfirmed"
        if old_handler is not None:
            signal.signal(signal.SIGINT, old_handler)
        if cancellation:
            result.update(outcome="incomplete", coverage="incomplete", reason="cancelled")
        result["findings"] = list(dict.fromkeys(records.findings + result["findings"]))[:3]
        result["completed"] = list(dict.fromkeys(records.completed + result["completed"]))
        result["skipped"] = {**records.skipped, **result["skipped"]}
        if records.stage_index >= 0:
            result["last_stage"] = STAGES[records.stage_index]
        result.update(cleanup=cleanup, termination=action, pid=proc.pid if proc else None,
                      elapsed_seconds=time.monotonic() - start)
    return result


def database_exit_code(result):
    if result["reason"] == "cancelled":
        return 130
    if result["findings"] or result["outcome"] == "corruption":
        return 1
    if result["outcome"] == "error" or result["cleanup"] != "confirmed":
        return 4
    if result["outcome"] != "pass":
        return 3
    return 0


def _worker_main():
    cancelled = False

    def cancel(signum, frame):
        nonlocal cancelled
        cancelled = True

    signal.signal(signal.SIGINT, cancel)

    def emit(record):
        print(json.dumps({"v": 1, **record}, separators=(",", ":")), flush=True)

    path, mode, deadline = sys.argv[1:]
    result = probe_database(path, mode, float(deadline), emit, cancelled=lambda: cancelled)
    emit({"type": "result", "result": result})


if __name__ == "__main__":
    _worker_main()
