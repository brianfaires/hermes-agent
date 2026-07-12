"""Read-only observation reconciliation and durable repair findings.

This module deliberately records evidence and fails closed; it performs no
Discord, Kanban, mapping, archive, or delete repair action.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True)
class ObservedThread:
    thread_id: str
    starter_message_id: str | None
    starter_revision_hash: str | None
    transition_message_ids: frozenset[str] = frozenset()
    title: str | None = None
    tags: tuple[str, ...] = ()
    archived: bool | None = None


@dataclass(frozen=True)
class ExpectedThread:
    title: str
    tags: tuple[str, ...]
    terminal: bool = False


@dataclass(frozen=True)
class ObservedDigest:
    thread_id: str
    content: str
    pinned: bool


@dataclass(frozen=True)
class ReconciliationFinding:
    finding_key: str
    severity: str
    code: str
    thread_id: str
    binding_key: str | None
    task_id: str | None
    evidence: dict
    evidence_hash: str
    first_seen_at: int
    last_seen_at: int
    resolved_at: int | None


_QUARANTINE_CODES = {
    "binding.open_count", "binding.card_missing", "binding.mapping_missing",
    "thread.starter_mapping_mismatch",
    "starter.revision_mismatch", "starter.changed_without_transition_confirmation",
    "transition.confirmation_missing", "thread.premature_archive", "digest.thread_mismatch",
}


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _finding(row: sqlite3.Row) -> ReconciliationFinding:
    return ReconciliationFinding(
        finding_key=row["finding_key"], severity=row["severity"], code=row["code"],
        thread_id=row["thread_id"], binding_key=row["binding_key"], task_id=row["task_id"],
        evidence=json.loads(row["evidence"]), evidence_hash=row["evidence_hash"],
        first_seen_at=row["first_seen_at"], last_seen_at=row["last_seen_at"],
        resolved_at=row["resolved_at"],
    )


def list_reconciliation_findings(conn: sqlite3.Connection, *, open_only: bool = False,
                                 thread_id: str | None = None) -> list[ReconciliationFinding]:
    clauses, args = [], []
    if open_only: clauses.append("resolved_at IS NULL")
    if thread_id is not None: clauses.append("thread_id=?"); args.append(str(thread_id))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return [_finding(r) for r in conn.execute(
        "SELECT * FROM mirror_reconciliation_findings" + where +
        " ORDER BY resolved_at IS NOT NULL,severity DESC,thread_id,code", args)]


def reconciliation_report(conn: sqlite3.Connection) -> dict:
    findings = list_reconciliation_findings(conn, open_only=True)
    return {
        "open_count": len(findings),
        "quarantined_threads": [r[0] for r in conn.execute(
            "SELECT thread_id FROM mirror_thread_quarantine WHERE resolved_at IS NULL ORDER BY thread_id")],
        "by_severity": {severity: sum(f.severity == severity for f in findings)
                        for severity in ("critical", "error", "warning")},
    }


def resolve_thread_quarantine(conn: sqlite3.Connection, thread_id: str, *, now: int | None = None) -> bool:
    """Clear a latched quarantine only after a reconciliation scan is clean."""
    stamp = int(time.time()) if now is None else int(now)
    thread = str(thread_id)
    marks = ",".join("?" for _ in _QUARANTINE_CODES)
    if conn.execute(
        f"SELECT 1 FROM mirror_reconciliation_findings WHERE thread_id=? AND resolved_at IS NULL AND code IN ({marks}) LIMIT 1",
        (thread, *_QUARANTINE_CODES),
    ).fetchone():
        return False
    changed = conn.execute(
        "UPDATE mirror_thread_quarantine SET needs_repair=0,resolved_at=?,updated_at=? WHERE thread_id=? AND resolved_at IS NULL",
        (stamp, stamp, thread),
    ).rowcount
    conn.commit()
    return bool(changed)


def reconcile_mirror_state(conn: sqlite3.Connection, *, observed_threads: Mapping[str, ObservedThread],
                           cards: Iterable[tuple[str, str]], now: int | None = None,
                           expected_threads: Mapping[str, ExpectedThread] | None = None,
                           observed_digest: ObservedDigest | None = None,
                           digest_observation_complete: bool = True) -> list[ReconciliationFinding]:
    """Atomically reconcile supplied snapshots; never performs a live repair."""
    stamp = int(time.time()) if now is None else int(now)
    known_cards = {(str(board), str(task)) for board, task in cards}
    expected_threads = expected_threads or {}
    detected: dict[tuple[str, str, str | None, str | None], tuple[str, dict]] = {}
    # Serialize the state snapshot and finding update.  Otherwise an older scan
    # can commit after a newer scan and re-open stale quarantine evidence.
    conn.execute("BEGIN IMMEDIATE")

    def add(severity: str, code: str, thread: str, binding: str | None, task: str | None, **evidence):
        detected[(thread, code, binding, task)] = (severity, {"thread_id": thread, **evidence})

    registry = conn.execute("SELECT id,thread_id,starter_message_id FROM mirror_initiatives WHERE kind='post' AND thread_id IS NOT NULL").fetchall()
    threads = sorted({str(r["thread_id"]) for r in registry} | {str(t) for t in observed_threads})
    for thread in threads:
        mappings = [r for r in registry if str(r["thread_id"]) == thread]
        epochs = conn.execute("SELECT * FROM mirror_binding_epochs WHERE thread_id=? ORDER BY sequence", (thread,)).fetchall()
        opens = [r for r in epochs if r["state"] == "open"]
        if len(opens) != 1:
            add("critical", "binding.open_count", thread, None, None, open_count=len(opens), binding_keys=[r["binding_key"] for r in opens])
        active = opens[0] if len(opens) == 1 else None
        if active is not None:
            binding, task = active["binding_key"], active["task_id"]
            if (active["board_slug"], task) not in known_cards:
                add("error", "binding.card_missing", thread, binding, task, board_slug=active["board_slug"])
            member_tasks = []
            if len(mappings) == 1:
                member_tasks = [r[0] for r in conn.execute("SELECT task_id FROM mirror_members WHERE initiative_id=? ORDER BY task_id", (mappings[0]["id"],))]
            if len(mappings) != 1 or member_tasks != [task]:
                add("error", "binding.mapping_missing", thread, binding, task, registry_count=len(mappings), mapped_tasks=member_tasks)
        observed = observed_threads.get(thread)
        if observed is not None and (len(mappings) != 1 or mappings[0]["starter_message_id"] != observed.starter_message_id):
            add("error", "thread.starter_mapping_mismatch", thread, None, None,
                registry_count=len(mappings),
                expected_starter_message_id=mappings[0]["starter_message_id"] if len(mappings) == 1 else None,
                observed_starter_message_id=observed.starter_message_id)
        transitions = conn.execute("SELECT * FROM mirror_binding_transitions WHERE thread_id=?", (thread,)).fetchall()
        for transition in transitions:
            state, key = transition["state"], transition["transition_key"]
            if state != "starter_verified":
                add("warning", "transition.pending", thread, transition["new_binding_key"], None, transition_key=key, state=state)
            message_id = transition["transition_message_id"]
            if state in {"message_confirmed", "starter_verified"} and observed is not None and message_id not in observed.transition_message_ids:
                add("error", "transition.confirmation_missing", thread, transition["new_binding_key"], None, transition_key=key, transition_message_id=message_id)
            if state == "prepared" and observed is not None and active is not None and observed.starter_revision_hash != active["starter_revision_hash"]:
                add("critical", "starter.changed_without_transition_confirmation", thread, active["binding_key"], active["task_id"], transition_key=key, expected_hash=active["starter_revision_hash"], observed_hash=observed.starter_revision_hash)
        if active is not None and observed is not None and active["starter_revision_hash"] is not None and observed.starter_revision_hash != active["starter_revision_hash"]:
            add("error", "starter.revision_mismatch", thread, active["binding_key"], active["task_id"], expected_hash=active["starter_revision_hash"], observed_hash=observed.starter_revision_hash)

        expected = expected_threads.get(thread)
        lifecycle = conn.execute("SELECT * FROM mirror_terminal_lifecycles WHERE thread_id=? ORDER BY prepared_at DESC LIMIT 1", (thread,)).fetchone()
        if expected is not None and observed is not None:
            binding = active["binding_key"] if active else None; task = active["task_id"] if active else None
            if observed.title != expected.title:
                add("warning", "thread.title_mismatch", thread, binding, task, expected=expected.title, observed=observed.title)
            if set(observed.tags) != set(expected.tags):
                add("warning", "thread.tags_mismatch", thread, binding, task, expected=sorted(expected.tags), observed=sorted(observed.tags))
            done = "done" in {tag.lower() for tag in observed.tags}
            if expected.terminal and not done:
                add("warning", "thread.done_tag_missing", thread, binding, task)
            if not expected.terminal and done:
                add("warning", "thread.done_tag_unexpected", thread, binding, task)
            completed = expected.terminal and lifecycle is not None and lifecycle["state"] == "archived"
            if observed.archived and not completed:
                add("critical", "thread.premature_archive", thread, binding, task, lifecycle_state=lifecycle["state"] if lifecycle else None)
            if completed and not observed.archived:
                add("error", "thread.unexpected_reopen", thread, binding, task)
            elif lifecycle is not None and lifecycle["state"] == "tag_confirmed" and stamp >= int(lifecycle["archive_due_at"] or stamp + 1) and not observed.archived:
                add("warning", "thread.terminal_unarchived", thread, binding, task, archive_due_at=lifecycle["archive_due_at"])

        if lifecycle is not None and lifecycle["state"] not in {"prepared", "summary_confirmed", "cancelled"}:
            digest = json.loads(lifecycle["frozen_payload"])["digest"]
            marker = f"<!-- terminal:{thread} -->"
            date = digest["date_range"].get("end") or digest["date_range"].get("start") or "?"
            expected_line = f"- [{date}]({digest.get('thread_link')}) — {digest.get('outcome') or 'completed'}"
            if observed_digest is None and digest_observation_complete:
                add("warning", "digest.entry_missing", thread, lifecycle["binding_key"], active["task_id"] if active else None,
                    reason="digest observation unavailable")
            if observed_digest is not None:
                binding = lifecycle["binding_key"]; task = active["task_id"] if active else None
                if observed_digest.thread_id == thread:
                    add("critical", "digest.thread_mismatch", thread, binding, task, digest_thread_id=observed_digest.thread_id)
                lines = observed_digest.content.splitlines(); pos = next((i for i, line in enumerate(lines) if line == marker), None)
                line = lines[pos + 1] if pos is not None and pos + 1 < len(lines) else None
                if line is None:
                    add("warning", "digest.entry_missing", thread, binding, task)
                elif line != expected_line:
                    if str(digest.get("thread_link")) not in line: add("error", "digest.thread_link_mismatch", thread, binding, task)
                    if str(digest.get("outcome") or "completed") not in line: add("warning", "digest.outcome_mismatch", thread, binding, task)
                    if f"[{date}]" not in line: add("warning", "digest.date_hash_mismatch", thread, binding, task, expected_hash=hashlib.sha256(str(date).encode()).hexdigest())
                    add("warning", "digest.entry_stale", thread, binding, task)
                if not observed_digest.pinned: add("warning", "digest.unpinned", thread, binding, task)

    # Optional additive delivery tables may be initialized by their owners.
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "mirror_discord_outbox" in tables:
        for r in conn.execute("SELECT operation_id,thread_id,status,attempt_count,last_error FROM mirror_discord_outbox WHERE status!='delivered'"):
            add("warning" if r["status"] in {"pending", "sending"} else "error", "delivery.outbound_" + ("failed" if r["last_error"] else "pending"), r["thread_id"], None, None, operation_id=r["operation_id"], status=r["status"], attempts=r["attempt_count"], error=r["last_error"])
    for r in conn.execute("SELECT operation_id,thread_id,task_id,status,attempt_count,last_error FROM mirror_conversation_deliveries WHERE status!='delivered'"):
        add("error" if r["status"] == "failed" else "warning", "delivery.log_" + ("failed" if r["status"] == "failed" else "pending"), r["thread_id"], None, r["task_id"], operation_id=r["operation_id"], status=r["status"], attempts=r["attempt_count"], error=r["last_error"])

    # A snapshot may intentionally be partial (for example, a failed Discord
    # page fetch).  Lack of an observation is not evidence of resolution.
    observed_codes = {
        "thread.starter_mapping_mismatch", "starter.revision_mismatch",
        "starter.changed_without_transition_confirmation", "transition.confirmation_missing",
        "thread.title_mismatch", "thread.tags_mismatch", "thread.done_tag_missing",
        "thread.done_tag_unexpected", "thread.premature_archive", "thread.unexpected_reopen",
        "thread.terminal_unarchived",
    }
    digest_codes = {"digest.thread_mismatch", "digest.entry_missing", "digest.thread_link_mismatch",
                    "digest.outcome_mismatch", "digest.date_hash_mismatch", "digest.entry_stale", "digest.unpinned"}
    for row in conn.execute("SELECT * FROM mirror_reconciliation_findings WHERE resolved_at IS NULL"):
        if row["code"] in observed_codes and row["thread_id"] not in observed_threads:
            evidence = json.loads(row["evidence"])
            detected[(row["thread_id"], row["code"], row["binding_key"], row["task_id"])] = (
                row["severity"], evidence,
            )
        if row["code"] in digest_codes and (not digest_observation_complete or observed_digest is None):
            evidence = json.loads(row["evidence"])
            detected[(row["thread_id"], row["code"], row["binding_key"], row["task_id"])] = (row["severity"], evidence)

    try:
        seen_keys = []
        for (thread, code, binding, task), (severity, evidence) in detected.items():
            identity = _canonical({"code": code, "thread": thread, "binding": binding, "task": task})
            key = hashlib.sha256(identity.encode()).hexdigest()
            payload = _canonical(evidence); evidence_hash = hashlib.sha256(payload.encode()).hexdigest()
            conn.execute("""INSERT INTO mirror_reconciliation_findings
                (finding_key,severity,code,thread_id,binding_key,task_id,evidence,evidence_hash,first_seen_at,last_seen_at)
                VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(finding_key) DO UPDATE SET
                severity=excluded.severity,evidence=excluded.evidence,evidence_hash=excluded.evidence_hash,
                last_seen_at=MAX(mirror_reconciliation_findings.last_seen_at,excluded.last_seen_at),resolved_at=NULL""",
                (key,severity,code,thread,binding,task,payload,evidence_hash,stamp,stamp))
            seen_keys.append(key)
        if seen_keys:
            marks = ",".join("?" for _ in seen_keys)
            conn.execute(f"UPDATE mirror_reconciliation_findings SET resolved_at=? WHERE resolved_at IS NULL AND finding_key NOT IN ({marks})", (stamp, *seen_keys))
        else:
            conn.execute("UPDATE mirror_reconciliation_findings SET resolved_at=? WHERE resolved_at IS NULL", (stamp,))
        bad_threads = {thread for (thread, code, _, _), _ in detected.items() if code in _QUARANTINE_CODES}
        for thread in bad_threads:
            conn.execute("""INSERT INTO mirror_thread_quarantine(thread_id,needs_repair,quarantined_at,updated_at)
                VALUES (?,1,?,?) ON CONFLICT(thread_id) DO UPDATE SET
                needs_repair=1,
                quarantined_at=CASE WHEN mirror_thread_quarantine.resolved_at IS NOT NULL
                                    THEN excluded.quarantined_at ELSE mirror_thread_quarantine.quarantined_at END,
                updated_at=excluded.updated_at,resolved_at=NULL""", (thread, stamp, stamp))
        # Quarantine remains latched after a clean scan.  Explicit operator
        # acknowledgement through resolve_thread_quarantine is required.
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return list_reconciliation_findings(conn, open_only=True)