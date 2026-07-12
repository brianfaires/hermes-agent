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
    "transition.confirmation_missing",
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


def reconcile_mirror_state(conn: sqlite3.Connection, *, observed_threads: Mapping[str, ObservedThread],
                           cards: Iterable[tuple[str, str]], now: int | None = None) -> list[ReconciliationFinding]:
    """Atomically reconcile supplied snapshots; never performs a live repair."""
    stamp = int(time.time()) if now is None else int(now)
    known_cards = {(str(board), str(task)) for board, task in cards}
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
    }
    for row in conn.execute("SELECT * FROM mirror_reconciliation_findings WHERE resolved_at IS NULL"):
        if row["code"] in observed_codes and row["thread_id"] not in observed_threads:
            evidence = json.loads(row["evidence"])
            detected[(row["thread_id"], row["code"], row["binding_key"], row["task_id"])] = (
                row["severity"], evidence,
            )

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
                VALUES (?,1,?,?) ON CONFLICT(thread_id) DO UPDATE SET needs_repair=1,updated_at=excluded.updated_at,resolved_at=NULL""", (thread, stamp, stamp))
        if bad_threads:
            marks = ",".join("?" for _ in bad_threads)
            conn.execute(f"UPDATE mirror_thread_quarantine SET needs_repair=0,resolved_at=?,updated_at=? WHERE resolved_at IS NULL AND thread_id NOT IN ({marks})", (stamp, stamp, *bad_threads))
        else:
            conn.execute("UPDATE mirror_thread_quarantine SET needs_repair=0,resolved_at=?,updated_at=? WHERE resolved_at IS NULL", (stamp, stamp))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return list_reconciliation_findings(conn, open_only=True)