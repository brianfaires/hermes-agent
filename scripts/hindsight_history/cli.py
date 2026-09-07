"""Local, recorded-only FC-37 evidence reader. No provider imports or setup."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys


class Refused(Exception):
    """Contains only a fixed, content-free diagnostic code."""


def private_stat(info: os.stat_result, *, directory: bool = False) -> None:
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or (not directory and info.st_nlink != 1)):
        raise Refused("authorization-denied")


def open_directory(path: str) -> int:
    """Check the owner boundary without listing ancestors; pin the directory."""
    if not path.startswith("/") or ".." in Path(path).parts:
        raise Refused("authorization-denied")
    target = Path(path)
    protected = False
    # In a user namespace the filesystem root's uid may be unmapped (65534).
    root_owner = Path("/").lstat().st_uid
    for ancestor in reversed([target, *target.parents]):
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (root_owner, os.geteuid()):
            raise Refused("authorization-denied")
        # A private owner directory above a group-writable child already keeps
        # other users out. Sticky system temp roots also protect owned children.
        if not protected and info.st_mode & 0o022:
            if not (info.st_uid == root_owner and info.st_mode & stat.S_ISVTX):
                raise Refused("authorization-denied")
        if info.st_uid == os.geteuid() and not info.st_mode & 0o077:
            protected = True
    private_stat(info, directory=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        private_stat(opened, directory=True)
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise Refused("authorization-denied")
        return fd
    except BaseException:
        os.close(fd)
        raise


def authorize(profile: str) -> int:
    if os.name != "posix" or os.getuid() != os.geteuid() or os.geteuid() == 0:
        raise Refused("authorization-denied")
    context = os.environ.get("HERMES_HOME", "")
    if not context or profile != context:
        raise Refused("profile-context-mismatch")
    # HERMES_HOME selects the context; OS ownership/private permissions authorize it.
    return open_directory(context)


def open_output(path: str) -> int:
    target = Path(path)
    parent = open_directory(str(target.parent))
    try:
        return os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                       0o600, dir_fd=parent)
    finally:
        os.close(parent)


def no_journal(directory: int) -> None:
    for name in ("state.db-wal", "state.db-journal"):
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size:
            raise Refused("recorded-evidence-unavailable-journal-present")


def load_snapshot(directory: int) -> bytes:
    """Read an offline snapshot, never open the profile through SQLite's VFS.

    Refuse outstanding journals instead of silently omitting WAL evidence. Pin
    the inode, lock out rollback writers, and reject changes during the read.
    """
    import fcntl

    no_journal(directory)
    fd = os.open("state.db", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        private_stat(os.fstat(fd))
        # SQLite's shared lock range; prevents rollback-mode commits during copy.
        fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB, 512, 0x40000000)
        before = os.fstat(fd)
        if before.st_size > 256 * 1024 * 1024:
            raise Refused("recorded-evidence-unavailable-size-limit")
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise Refused("recorded-evidence-unavailable-changed")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        no_journal(directory)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise Refused("recorded-evidence-unavailable-changed")
        data = b"".join(chunks)
        # A checkpointed WAL database needs rollback header flags for an in-memory
        # deserialize (there is deliberately no filesystem WAL to open here).
        if not data.startswith(b"SQLite format 3\x00") or len(data) < 100:
            raise Refused("recorded-evidence-corrupt")
        data = data[:18] + b"\x01\x01" + data[20:]
        return data
    finally:
        os.close(fd)


NAMES = {"hindsight_recall", "hindsight_retain", "hindsight_reflect"}


def recorded(rows: list[sqlite3.Row]) -> dict:
    """Return literal persisted evidence, never infer automatic memory activity."""
    events = []
    corrupt = False
    for row in rows:
        if row["role"] == "assistant" and row["tool_calls"] is not None:
            try:
                calls = json.loads(row["tool_calls"])
                if not isinstance(calls, list):
                    raise ValueError
                for call in calls:
                    if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                        raise ValueError
                    function = call["function"]
                    if function.get("name") in NAMES:
                        arguments = function.get("arguments")
                        status = "recorded"
                        try:
                            decoded = json.loads(arguments) if isinstance(arguments, str) else arguments
                            if not isinstance(decoded, dict):
                                raise ValueError
                        except (ValueError, TypeError):
                            status = "corrupt"
                            corrupt = True
                        events.append({"message_id": row["id"], "kind": "tool_call",
                                       "name": function["name"], "call_id": call.get("id") or call.get("call_id"),
                                       "arguments": arguments, "status": status})
            except (ValueError, TypeError):
                corrupt = True
                events.append({"message_id": row["id"], "kind": "tool_calls", "status": "corrupt"})
        elif row["role"] == "tool":
            # Do not guess a name or pair ambiguous/reused IDs with another turn.
            matches = [e for e in events if e.get("kind") == "tool_call"
                       and e.get("call_id") and e["call_id"] == row["tool_call_id"]]
            name = row["tool_name"]
            if name in NAMES or matches:
                events.append({"message_id": row["id"], "kind": "tool_result",
                               "name": name if name in NAMES else None,
                               "call_id": row["tool_call_id"], "content": row["content"],
                               "status": "missing" if row["content"] is None else
                               "empty" if row["content"] == "" else "recorded"})
    for event in events:
        if event["kind"] == "tool_call":
            call_id = event.get("call_id")
            calls = [e for e in events if e["kind"] == "tool_call" and e.get("call_id") == call_id]
            results = [e for e in events if e["kind"] == "tool_result" and e.get("call_id") == call_id
                       and e["message_id"] > event["message_id"]]
            event["result_status"] = (
                "ambiguous" if not call_id or len(calls) != 1 or len(results) > 1 else
                results[0]["status"] if results else "missing")
    return {"status": "corrupt" if corrupt else "recorded" if events else "empty",
            "events": events}


def report(data: bytes, session: str, profile: str, reconstruct: bool) -> dict:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.deserialize(data)
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        # Reject views/triggers masquerading as the expected persisted tables.
        tables = dict(conn.execute("SELECT name, type FROM sqlite_master WHERE name IN ('sessions','messages')"))
        if tables.get("sessions") != "table":
            raise Refused("recorded-evidence-corrupt")
        row = conn.execute("SELECT profile_name FROM sessions WHERE id = ?", (session,)).fetchone()
        if row is None:
            raise Refused("session-not-authorized-or-missing")
        profile_name = Path(profile).name if Path(profile).parent.name == "profiles" else "default"
        if row["profile_name"] not in (None, "", profile_name):
            raise Refused("session-profile-mismatch")
        result = {"mode": "recorded evidence", "session": session,
                  "session_record": "present", "automatic_activity": "unknown: not inferred",
                  "reconstruction": "not requested", "messages": "missing",
                  "evidence": {"status": "missing", "events": []}}
        if reconstruct:
            result["reconstruction"] = (
                "refused: current backend API cannot enforce requested profile/session scope; "
                "no query performed; no reconstructed content produced")
        if tables.get("messages") == "table":
            try:
                rows = conn.execute(
                    "SELECT id, role, tool_calls, tool_call_id, tool_name, content FROM messages "
                    "WHERE session_id = ? AND (active = 1 OR compacted = 1) ORDER BY id", (session,)
                ).fetchall()
                result["messages"] = "recorded" if rows else "empty"
                result["evidence"] = recorded(rows)
            except sqlite3.Error:
                result["messages"] = "corrupt"
                result["evidence"]["status"] = "corrupt"
        elif "messages" in tables:
            result["messages"] = "corrupt"
            result["evidence"]["status"] = "corrupt"
        return result


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's normal error includes rejected arguments, possibly private.
        raise Refused("invalid-arguments")


def main(argv: list[str] | None = None) -> int:
    try:
        parser = Parser(description="Export recorded Hindsight evidence to a new private JSON file.")
        parser.add_argument("--profile-home", required=True)
        parser.add_argument("--session", required=True)
        parser.add_argument("--output", required=True)
        parser.add_argument("--reconstruct", action="store_true", help="Explicit request; refused until backend scope is enforceable.")
        args = parser.parse_args(argv)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", args.session):
            raise Refused("invalid-session")
        directory = authorize(args.profile_home)
        try:
            output = open_output(args.output)  # private destination before any data read
            with os.fdopen(output, "w", encoding="utf-8") as stream:
                try:
                    data = load_snapshot(directory)
                except FileNotFoundError:
                    raise Refused("recorded-evidence-missing-database") from None
                result = report(data, args.session, args.profile_home, args.reconstruct)
                json.dump(result, stream, ensure_ascii=True, indent=2)
                stream.write("\n")
        finally:
            os.close(directory)
        print("private-report-written")
        return 3 if args.reconstruct else 0
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except sqlite3.DatabaseError:
        print("recorded-evidence-corrupt", file=sys.stderr)
        return 2
    except Exception:
        # Never log exceptions: SQLite, OS and backend exceptions may contain content.
        print("recorded-evidence-unavailable", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
