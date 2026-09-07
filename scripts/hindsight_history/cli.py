"""Scoped local evidence and explicit current-memory recovery. No provider setup."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
import inspect
import json
import logging
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


def private_file(directory: int, name: str) -> int:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        private_stat(os.fstat(fd))
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def open_database(directory: int, profile: str):
    """Use SQLite's own WAL reader with read-only DB *and* shared memory.

    The Unix VFS's readonly_shm option avoids read-mark writes and sidecar
    creation. SQLite handles locks, WAL checksums and transaction consistency;
    unavailable/recovery-required journals fail closed without a snapshot.
    This standalone CLI must run in its own process (no shared writable VFS).
    """
    if sqlite3.sqlite_version_info < (3, 22, 0):
        raise Refused("recorded-evidence-unavailable-sqlite-version")
    fd = private_file(directory, "state.db")
    try:
        sizes = {}
        for name in ("state.db-wal", "state.db-shm", "state.db-journal"):
            try:
                sidecar = private_file(directory, name)
            except FileNotFoundError:
                continue
            with os.fdopen(sidecar, "rb") as stream:
                sizes[name] = os.fstat(stream.fileno()).st_size
                if name.endswith("-journal") and sizes[name]:
                    raise Refused("recorded-evidence-unavailable-journal-present")
        if sizes.get("state.db-wal") and not sizes.get("state.db-shm"):
            raise Refused("recorded-evidence-unavailable-wal-without-shm")
        # Retain the authorized descriptor and verify the path SQLite will open.
        info = os.stat(Path(profile) / "state.db", follow_symlinks=False)
        pinned = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise Refused("authorization-denied")
        uri = (Path(profile) / "state.db").as_uri()
        with closing(sqlite3.connect(
                uri + "?mode=ro&readonly_shm=1&vfs=unix&cache=private", uri=True,
                timeout=1)) as conn:
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if sizes.get("state.db-wal") and mode != "wal":
                raise Refused("recorded-evidence-unavailable-journal-state")
            conn.row_factory = sqlite3.Row
            yield conn
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


def profile_name(profile: str) -> str:
    path = Path(profile)
    return path.name if path.parent.name == "profiles" else "default"


def report(conn: sqlite3.Connection, session: str, profile: str) -> dict:
    # Reject views/triggers masquerading as the expected persisted tables.
    tables = dict(conn.execute("SELECT name, type FROM sqlite_master WHERE name IN ('sessions','messages')"))
    if tables.get("sessions") != "table":
        raise Refused("recorded-evidence-corrupt")
    row = conn.execute("SELECT profile_name FROM sessions WHERE id = ?", (session,)).fetchone()
    if row is None:
        raise Refused("session-not-authorized-or-missing")
    name = profile_name(profile)
    if row["profile_name"] not in (None, "", name):
        raise Refused("session-profile-mismatch")
    result = {"mode": "recorded evidence", "session": session,
              "session_record": "present", "automatic_activity": "unknown: not inferred",
              "reconstruction": "not requested", "messages": "missing",
              "evidence": {"status": "missing", "events": []}}
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


CURRENT_MEMORY_LABEL = "reconstructed from current memory NOT original transcript"
SAFE_BANK_ID = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*\Z")


def validate_bank_id(bank_id: object) -> str:
    if (not isinstance(bank_id, str) or not SAFE_BANK_ID.fullmatch(bank_id)
            or len(bank_id) > 200):
        raise Refused("reconstruction-profile-bank-not-isolated")
    return bank_id


def resolve_bank_id(config: dict, profile: str) -> str:
    template = config.get("bank_id_template")
    if template not in (None, ""):
        if not isinstance(template, str):
            raise Refused("reconstruction-profile-bank-not-isolated")
        name = profile_name(profile)
        if (template.count("{profile}") != 1
                or not re.fullmatch(r"[A-Za-z0-9_-]*\{profile\}[A-Za-z0-9_-]*", template)
                or not re.fullmatch(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*", name)):
            raise Refused("reconstruction-profile-bank-not-isolated")
        bank = template.replace("{profile}", name)
        return validate_bank_id(bank)

    bank = config.get("bank_id")
    if bank in (None, ""):
        banks = config.get("banks")
        if isinstance(banks, dict):
            hermes = banks.get("hermes")
            if isinstance(hermes, dict):
                bank = hermes.get("bankId")
    if bank in (None, ""):
        raise Refused("reconstruction-config-unavailable")
    return validate_bank_id(bank)


def reconstruction_config(directory: int, profile: str) -> tuple[str, str, str | None]:
    """Read only the selected provider's persisted config, never its fallbacks."""
    subdir = os.open("hindsight", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                     dir_fd=directory)
    try:
        private_stat(os.fstat(subdir), directory=True)
        with os.fdopen(private_file(subdir, "config.json"), "r", encoding="utf-8") as stream:
            config = json.load(stream)
    finally:
        os.close(subdir)
    if not isinstance(config, dict):
        raise Refused("reconstruction-config-unavailable")
    bank = resolve_bank_id(config, profile)
    mode = config.get("mode", "cloud")
    # Match the provider's existing cloud/external URL defaults. Embedded mode
    # needs its daemon manager to resolve/start a service and is unsupported here.
    url = config.get("api_url", "https://api.hindsight.vectorize.io" if mode == "cloud"
                     else "http://localhost:8888")
    key = config.get("apiKey") or config.get("api_key") or None
    if not key:
        try:
            secret_fd = private_file(directory, ".env")
        except FileNotFoundError:
            pass
        else:
            try:
                from agent.secret_scope import load_env_file
                # Only this profile's key; no environment interpolation or
                # fallback to the process/default profile's credentials.
                key = load_env_file(Path(profile) / ".env").get("HINDSIGHT_API_KEY")
            finally:
                os.close(secret_fd)
    if (mode not in ("cloud", "local_external") or not isinstance(url, str)
            or not url.startswith(("http://", "https://"))
            or (key is not None and not isinstance(key, str))
            or (mode == "cloud" and not key)):
        raise Refused("reconstruction-config-unavailable")
    return bank, url, key


def validate_memories(response, session: str, profile: str) -> list[dict]:
    """Validate the complete response before emitting any fresh fact text."""
    if any(getattr(response, field, None) for field in
           ("entities", "chunks", "source_facts", "trace")):
        raise Refused("reconstruction-provenance-denied")
    results = getattr(response, "results", None)
    if not isinstance(results, list):
        raise Refused("reconstruction-provenance-denied")
    facts = []
    for item in results:
        tags = getattr(item, "tags", None)
        metadata = getattr(item, "metadata", None)
        if (not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)
                or {t for t in tags if t.startswith("session:")} != {f"session:{session}"}
                or not isinstance(metadata, dict) or metadata.get("session_id") != session
                or metadata.get("agent_identity") != profile_name(profile)
                or getattr(item, "type", None) not in ("world", "experience")
                or getattr(item, "source_fact_ids", None)
                or not isinstance(getattr(item, "id", None), str) or not item.id
                or not isinstance(getattr(item, "text", None), str)):
            raise Refused("reconstruction-provenance-denied")
        # Deliberately exclude contexts, entities, raw chunks, and arbitrary
        # metadata. The exact session tag + provider metadata are the provenance.
        facts.append({"id": item.id, "type": item.type, "text": item.text,
                      "session": session, "profile": profile_name(profile),
                      "label": CURRENT_MEMORY_LABEL})
    return facts


def reconstruct(directory: int, session: str, profile: str) -> dict:
    result = {"label": CURRENT_MEMORY_LABEL, "status": "unavailable", "facts": []}
    # Optional SDK/transport diagnostics can contain response bodies or keys.
    # This is a standalone command; silence them only across this boundary.
    previous = logging.root.manager.disable
    try:
        logging.disable(sys.maxsize)
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            try:
                bank, url, key = reconstruction_config(directory, profile)
            except Refused:
                raise
            except Exception:
                raise Refused("reconstruction-config-unavailable") from None
            try:
                from hindsight_client import Hindsight
            except ImportError:
                raise Refused("reconstruction-sdk-unavailable") from None
            arguments = dict(
                bank_id=bank, query="Recall the facts and events from this session.",
                types=["world", "experience"], tags=[f"session:{session}"],
                tags_match="all_strict", include_entities=False, include_chunks=False,
                include_source_facts=False, trace=False, max_tokens=4096, budget="mid")
            try:
                inspect.signature(Hindsight.recall).bind(None, **arguments)
            except (TypeError, ValueError, AttributeError):
                raise Refused("reconstruction-sdk-incompatible") from None
            with Hindsight(base_url=url, api_key=key, timeout=30) as client:
                response = client.recall(**arguments)
                facts = validate_memories(response, session, profile)
        result.update(status="reconstructed" if facts else "empty", facts=facts)
    except Refused as exc:
        result["error"] = str(exc)
    except Exception:
        result["error"] = "reconstruction-backend-unavailable"
    finally:
        logging.disable(previous)
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
        parser.add_argument("--reconstruct", action="store_true", help="Query current memory once for this authorized profile/session.")
        args = parser.parse_args(argv)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", args.session):
            raise Refused("invalid-session")
        directory = authorize(args.profile_home)
        try:
            output = open_output(args.output)  # private destination before any data read
            with os.fdopen(output, "w", encoding="utf-8") as stream:
                try:
                    with open_database(directory, args.profile_home) as conn:
                        result = report(conn, args.session, args.profile_home)
                except FileNotFoundError:
                    raise Refused("recorded-evidence-missing-database") from None
                if args.reconstruct:
                    result["reconstruction"] = reconstruct(directory, args.session, args.profile_home)
                json.dump(result, stream, ensure_ascii=True, indent=2)
                stream.write("\n")
        finally:
            os.close(directory)
        print("private-report-written")
        return 3 if args.reconstruct and result["reconstruction"]["status"] == "unavailable" else 0
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except sqlite3.OperationalError:
        print("recorded-evidence-unavailable", file=sys.stderr)
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
