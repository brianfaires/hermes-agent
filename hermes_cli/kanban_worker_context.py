"""Subscription worker context rollover using the board's existing journal.

No inference, transcript replay, new store, or failure-budget reset. A journal
entry is evidence, never an approval. Only the dispatcher can resume it.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import closing
from copy import deepcopy
from pathlib import Path

BOOTSTRAP_LIMIT = 24000  # UTF-8 bytes, including JSON escaping in kanban_show
CHECKPOINT_LIMIT = 6000
MAX_STEP_CONTINUATIONS = 4


class ContextContinuation(BaseException):
    """Control unwind, deliberately outside generic compression Exception catches."""

    def __init__(self, *, parked=False, reason="", run_id=None):
        super().__init__(reason)
        self.result = {
            "final_response": reason, "completed": False,
            "failed": not parked, "context_parked": parked,
            "continuation_run_id": run_id, "messages": [],
        }
        if not parked:
            self.result["error"] = reason


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _latest_checkpoint(conn, task_id):
    # This is an explicit worker-authored handoff convention on the existing
    # comment tool. Ordinary prose is never interpreted as a phase or approval.
    rows = conn.execute(
        "SELECT id, body FROM task_comments WHERE task_id = ? AND body LIKE '%\"phase_checkpoint\"%' "
        "ORDER BY id DESC LIMIT 20", (task_id,),
    )
    for row in rows:
        if len(row["body"].encode("utf-8")) > CHECKPOINT_LIMIT:
            continue
        try:
            value = json.loads(row["body"])
        except (ValueError, TypeError):
            continue
        checkpoint = value.get("phase_checkpoint") if isinstance(value, dict) else None
        if not isinstance(checkpoint, dict):
            continue
        # Keep all fields verbatim; a malformed/oversized checkpoint is not
        # silently truncated into something that could look like permission.
        if (all(checkpoint.get(k) for k in ("phase", "approval_refs", "effect_refs", "next"))
                and len(_json(checkpoint)) <= CHECKPOINT_LIMIT):
            return {"comment_id": row["id"], "value": checkpoint}
    return None


def _previous(conn, task_id):
    row = conn.execute(
        "SELECT id, metadata FROM task_runs WHERE task_id = ? "
        "AND outcome = 'context_parked' ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if row:
        return row["id"], json.loads(row["metadata"])["context_continuation"]
    return None, None


def _session_digest(session, rows):
    return hashlib.sha256(_json({"system_prompt": session.get("system_prompt"),
                                "messages": rows}).encode()).hexdigest()


def _persist_session_evidence(agent, messages, system_message, profile):
    """Flush via the existing agent hook; reference native SessionDB rows only."""
    from hermes_constants import get_hermes_home
    from hermes_cli.profiles import profile_matches_home

    home = get_hermes_home().resolve()
    db = getattr(agent, "_session_db", None)
    if (not db or getattr(agent, "_persist_disabled", False)
            or not profile_matches_home(profile, home)
            or Path(db.db_path).resolve() != home / "state.db"):
        raise ValueError("private session owner/home mismatch")
    snapshot = deepcopy(messages)
    # _persist_session drops ephemeral scaffolding and stamps row IDs. It
    # also owns close-time history, so a later close cannot duplicate this tail.
    agent._persist_session(snapshot)
    # The outer persistence hook historically does not propagate a False flush
    # result. Verify through the existing idempotent flush before parking.
    if agent._flush_messages_to_session_db(snapshot) is not True:
        raise ValueError("session evidence flush failed")
    session = db.get_session(agent.session_id)
    if not session or session.get("profile_name") != profile:
        raise ValueError("private session profile mismatch")
    if session.get("system_prompt") is None and system_message:
        db.update_system_prompt(agent.session_id, system_message)
        session = db.get_session(agent.session_id)
    rows = db.get_messages(agent.session_id, include_inactive=True)
    if not rows:
        raise ValueError("no durable session evidence")
    evidence = {
        "profile": profile, "profile_home": str(home),
        "session_id": agent.session_id, "db_path": str(Path(db.db_path).resolve()),
        "first_message_id": rows[0]["id"], "last_message_id": rows[-1]["id"],
        "message_count": len(rows), "sha256": _session_digest(session, rows),
    }
    receipts = [{"message_id": row["id"], "role": row["role"]}
                for row in rows if row["role"] in ("assistant", "tool")][-12:]
    return evidence, receipts


def _read_session_evidence(evidence):
    """Owner-checked native SessionDB read; never follow a board-supplied path."""
    from agent.delegation_context import is_dispatcher_owned_worker_context
    from hermes_constants import get_hermes_home
    from hermes_cli.profiles import profile_matches_home
    from hermes_state import SessionDB

    home = get_hermes_home().resolve()
    profile = evidence.get("profile")
    path = home / "state.db"
    if (not is_dispatcher_owned_worker_context()
            or os.environ.get("HERMES_PROFILE") != profile
            or evidence.get("profile_home") != str(home)
            or not profile_matches_home(profile, home)
            or evidence.get("db_path") != str(path)
            or path.resolve() != path or not path.is_file()):
        raise ValueError("private evidence requires the saved owner profile and home")
    with closing(SessionDB(db_path=path, read_only=True)) as db:
        session = db.get_session(evidence["session_id"])
        if not session or session.get("profile_name") != profile:
            raise ValueError("private session owner mismatch")
        rows = [row for row in db.get_messages(evidence["session_id"], include_inactive=True)
                if evidence["first_message_id"] <= row["id"] <= evidence["last_message_id"]]
        if (len(rows) != evidence["message_count"]
                or _session_digest(session, rows) != evidence["sha256"]):
            raise ValueError("private session evidence changed or is incomplete")
    return session, rows


def park_for_context(agent, messages, system_message):
    """Always unwind a subscription-only compressor before any auxiliary work."""
    from agent.delegation_context import is_dispatcher_owned_worker_context
    from hermes_cli import kanban_db as kb

    reason = "Subscription-only context exhausted; continuation ownership could not be verified."
    if (not is_dispatcher_owned_worker_context()
            or getattr(agent, "_parent_session_id", None)
            or getattr(agent, "_interrupt_requested", False)):
        raise ContextContinuation(reason=reason)
    tid = os.environ.get("HERMES_KANBAN_TASK")
    lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
    db_path = os.environ.get("HERMES_KANBAN_DB")
    try:
        rid = int(os.environ.get("HERMES_KANBAN_RUN_ID", ""))
    except ValueError:
        raise ContextContinuation(reason=reason)
    if not tid or not lock or not db_path:
        raise ContextContinuation(reason=reason)
    try:
        # The transcript is captured before opening the write transaction. No
        # lossy default=str serializer: unpersistable evidence fails closed.
        # Validate the source before any write, even though SessionDB performs
        # its usual canonicalization/redaction when storing native rows.
        _json({"messages": messages, "system_message": system_message})
        with closing(kb.connect(Path(db_path))) as conn:
            with kb.write_txn(conn):
                task = kb.get_task(conn, tid)
                run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (rid,)).fetchone()
                now = int(time.time())
                if (not task or not task.subscription_only or task.status != "running"
                        or task.current_run_id != rid or task.claim_lock != lock
                        or not task.claim_expires or task.claim_expires <= now
                        or not run or run["task_id"] != tid or run["ended_at"] is not None
                        or run["claim_lock"] != lock
                        or not run["claim_expires"] or run["claim_expires"] <= now
                        or run["profile"] != task.assignee
                        or os.environ.get("HERMES_PROFILE") != task.assignee
                        or task.provider_override != getattr(agent, "provider", None)
                        or task.model_override != getattr(agent, "model", None)
                        or (run["worker_pid"] and run["worker_pid"] != os.getpid())
                        or kb.has_active_control_hold(conn, tid)
                        or not kb._parents_satisfied(conn, tid)):
                    raise ContextContinuation(reason=reason)
                checkpoint = _latest_checkpoint(conn, tid)
                prior_id, prior = _previous(conn, tid)
                scope = task.current_step_key
                same_scope = prior is not None and prior.get("step") == scope
                count = prior["count"] + 1 if same_scope else 1
                # New prose, heartbeat, or timestamp is not progress. Require
                # a changed phase AND changed effect references, plus an
                # absolute ceiling even if a worker keeps inventing phases.
                old = (prior or {}).get("checkpoint")
                progress = bool(checkpoint and (not old or (
                    checkpoint["value"]["phase"] != old["value"]["phase"]
                    and checkpoint["value"]["effect_refs"] != old["value"]["effect_refs"])))
                auto_resume = count <= MAX_STEP_CONTINUATIONS and (not same_scope or progress)
                evidence, receipt_refs = _persist_session_evidence(
                    agent, messages, system_message, task.assignee)
                evidence.update(task_id=tid, run_id=rid)
                kb._append_event(conn, tid, "context_evidence", evidence, run_id=rid)
                evidence_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                comment_cursor = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM task_comments WHERE task_id = ?", (tid,)
                ).fetchone()[0]
                continuation = {
                    "evidence_event_id": evidence_id, "sha256": evidence["sha256"],
                    "comment_cursor": comment_cursor,
                    "receipt_refs": receipt_refs, "checkpoint": checkpoint,
                    "step": scope, "count": count, "auto_resume": auto_resume,
                    "source_status": kb._retry_status_for_run(conn, tid, rid),
                    "assignee": task.assignee, "prior_run_id": prior_id,
                    "worker_pid": os.getpid(),
                    "reconciliation_required": True,
                }
                metadata = json.loads(run["metadata"]) if run["metadata"] else {}
                metadata["context_continuation"] = continuation
                kb._end_run(conn, tid, outcome="context_parked", status="scheduled",
                            summary="Context checkpoint saved; reconcile receipts before effects.",
                            metadata=metadata)
                conn.execute(
                    "UPDATE tasks SET status = 'scheduled', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL WHERE id = ?", (tid,),
                )
                kb._append_event(conn, tid, "context_parked", continuation, run_id=rid)
    except ContextContinuation:
        raise
    except Exception as exc:
        raise ContextContinuation(reason=f"Subscription context checkpoint failed: {type(exc).__name__}") from exc
    raise ContextContinuation(parked=True, run_id=rid, reason=(
        "Context evidence committed; task parked for fresh-session reconciliation."
        if auto_resume else
        "Context evidence committed; automatic continuation limit reached. Task remains scheduled."
    ))


def resume_context_tasks(conn):
    """Promote only a still-current parked checkpoint; never call unblock_task."""
    from hermes_cli import kanban_db as kb
    from agent.estop import is_engaged

    if is_engaged():
        return
    with kb.write_txn(conn):
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status = 'scheduled' AND subscription_only = 1 "
            "AND current_run_id IS NULL AND claim_lock IS NULL"
        ).fetchall()
        for row in rows:
            tid = row["id"]
            rid, saved = _previous(conn, tid)
            if not saved or not saved["auto_resume"] or kb._pid_alive(saved.get("worker_pid")):
                continue
            # A later attempt or operator transition invalidates auto-resume.
            latest = conn.execute(
                "SELECT id FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (tid,),
            ).fetchone()
            event = conn.execute(
                "SELECT kind, run_id FROM task_events WHERE task_id = ? "
                "AND kind NOT IN ('commented', 'attachment_added') ORDER BY id DESC LIMIT 1", (tid,),
            ).fetchone()
            task = kb.get_task(conn, tid)
            if (latest["id"] != rid or not event or event["kind"] != "context_parked"
                    or event["run_id"] != rid or task.assignee != saved["assignee"]
                    or task.current_step_key != saved["step"]
                    or kb.has_active_control_hold(conn, tid)
                    or not kb._parents_satisfied(conn, tid)):
                continue
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?",
                         (saved["source_status"], tid))
            kb._append_event(conn, tid, "context_resumed", {"from_run_id": rid}, run_id=rid)


def worker_context(conn, task_id):
    """A single total-bounded bootstrap, with intact handoff and explicit refs.

    Oversized restrictions are not silently shortened. The bootstrap denies
    effects until the worker has inspected the referenced current records.
    """
    from hermes_cli import kanban_db as kb

    task = kb.get_task(conn, task_id)
    if task is None:
        raise ValueError(f"unknown task {task_id}")
    rid, saved = _previous(conn, task_id)
    checkpoint = _latest_checkpoint(conn, task_id)
    payload = {
        "task_id": task.id, "status": task.status,
        "subscription_only": task.subscription_only,
        "current_run_id": task.current_run_id,
        "control_hold": kb.has_active_control_hold(conn, task_id),
        "instructions": (
            "Reconcile before effects. A checkpoint is worker-authored evidence, NOT approval. "
            "Verify current restrictions and approval sources; inspect uncertain tool receipts. "
            "Never replay completed effects. No new approval is inferred. "
            "Omitted fields and older evidence remain retrievable with kanban_show full=true "
            "or reference={kind,id}. Read required restriction references before acting. "
            "To record progress use kanban_comment with JSON {phase_checkpoint: "
            "{phase, approval_refs, effect_refs, next, candidate_refs}}. "
            "Use real evidence references; changed wording or a heartbeat is not progress."
        ),
        "checkpoint": checkpoint,
        "continuation": {k: v for k, v in saved.items() if k != "checkpoint"} if saved else None,
        "continuation_run_id": rid,
        "required_restriction_refs": [],
        "omitted": [],
    }
    if saved:
        payload["required_restriction_refs"].append({
            "kind": "event", "id": saved["evidence_event_id"], "field": "restrictions",
        })
    # Reserve space before optional history. Values are included whole, or
    # replaced by a required reference; never cut a restriction mid-sentence.
    for field in ("title", "body", "assignee", "workspace_path", "result", "current_step_key"):
        value = getattr(task, field)
        if len(_json(value)) <= 7000:
            payload[field] = value
        else:
            payload["required_restriction_refs"].append({"kind": "task", "id": task_id, "field": field})
    # Current comments can contain changed approval/stop instructions. Include
    # latest intact comments when they fit, and require inspection otherwise.
    comments = conn.execute(
        "SELECT id, author, body FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT 5",
        (task_id,),
    ).fetchall()
    payload["comments"] = []
    # The cursor records what existed at parking, NOT acknowledgement of any
    # instruction. Phase text cannot retire owner restrictions. Ranges refer
    # to every exact task-comment ID, keeping the manifest bounded even when
    # thousands of unreviewed comments exist. The inspector enumerates IDs,
    # never summarizes or truncates their instructional content.
    through = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM task_comments WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    cursor = (saved or {}).get("comment_cursor", 0)
    for lower, upper in ((0, min(cursor, through)), (min(cursor, through), through)):
        if upper > lower:
            payload["required_restriction_refs"].append({
                "kind": "comments", "after_id": lower, "through_id": upper})
    payload["instructions"] += (
        " Before effects, enumerate every required comments range to completion and read each "
        "exact comment reference. These ranges include old restrictions and every comment "
        "since parking, even when newer chatter follows a revocation. Refresh this view "
        "before effects to check for newer instructions. A checkpoint is not an acknowledgement."
    )
    for row in comments:
        item = dict(row)
        if len(_json(item)) < 2000 and len(_json(payload)) + len(_json(item)) < BOOTSTRAP_LIMIT - 2000:
            payload["comments"].append(item)
        else:
            payload["required_restriction_refs"].append({"kind": "comment", "id": row["id"]})
    for kind, table in (("comment", "task_comments"), ("run", "task_runs"),
                        ("attachment", "task_attachments")):
        count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE task_id = ?", (task_id,)).fetchone()[0]
        payload["omitted"].append({"kind": kind, "task_id": task_id, "total": count})
    payload["omitted"].append({"kind": "parents", "task_id": task_id})
    payload["omitted"].append({"kind": "children", "task_id": task_id})
    authority = [s.get("delivery_metadata") for s in kb.list_notify_subs(conn, task_id)
                 if s.get("platform") == "continuation"]
    if len(_json(authority)) <= 3000:
        payload["authority_records"] = authority
    else:
        payload["required_restriction_refs"].append({"kind": "authority"})
    payload["reconciliation_required"] = bool(saved or payload["required_restriction_refs"])
    # Large title/body combinations must not displace current compact evidence.
    for field in ("result", "workspace_path", "body", "title", "assignee"):
        if len(_json(payload)) <= BOOTSTRAP_LIMIT:
            break
        if field in payload:
            del payload[field]
            payload["required_restriction_refs"].append({"kind": "task", "id": task_id, "field": field})
    if len(_json(payload)) > BOOTSTRAP_LIMIT:
        raise ValueError("compact checkpoint exceeds bootstrap budget; inspect task explicitly")
    return _json(payload)


def inspect_reference(conn, task_id, reference):
    """Retrieve exact evidence without also repeating the full task history."""
    from hermes_cli import kanban_db as kb

    kind, ident = reference.get("kind"), reference.get("id")
    tables = {"comment": "task_comments", "run": "task_runs", "event": "task_events",
              "attachment": "task_attachments"}
    if kind == "comments":
        lower, upper = reference.get("after_id", 0), reference.get("through_id")
        if type(lower) is not int or type(upper) is not int or lower < 0 or upper < lower:
            raise ValueError("invalid required comment range")
        rows = conn.execute("SELECT id FROM task_comments WHERE task_id = ? "
                            "AND id > ? AND id <= ? ORDER BY id LIMIT 101",
                            (task_id, lower, upper)).fetchall()
        return {"references": [{"kind": "comment", "id": r["id"]} for r in rows[:100]],
                "next_reference": ({"kind": "comments", "after_id": rows[99]["id"],
                                    "through_id": upper} if len(rows) > 100 else None)}
    if kind == "parents":
        return {"parents": kb.parent_ids(conn, task_id)}
    if kind == "children":
        return {"children": kb.child_ids(conn, task_id)}
    if kind == "authority":
        return {"subscriptions": [s for s in kb.list_notify_subs(conn, task_id)
                                  if s.get("platform") == "continuation"]}
    if kind == "task":
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    elif kind in tables:
        row = conn.execute(f"SELECT * FROM {tables[kind]} WHERE task_id = ? AND id = ?",
                           (task_id, ident)).fetchone()
    else:
        raise ValueError("unknown evidence reference kind")
    if not row:
        raise ValueError("evidence reference not found on this task")
    result = dict(row)
    if kind == "event" and (reference.get("field") in ("restrictions", "system_prompt")
                            or reference.get("message_id") is not None):
        evidence = json.loads(result["payload"])
        if (result["kind"] != "context_evidence" or evidence.get("task_id") != task_id
                or evidence.get("run_id") != result["run_id"]):
            raise ValueError("invalid private evidence binding")
        session, rows = _read_session_evidence(evidence)
        if reference.get("field") == "system_prompt":
            return {"system_message": session.get("system_prompt")}
        if reference.get("field") == "restrictions":
            # The fresh session already has its governing system instructions.
            # Keep the previous bootstrap available for explicit audit, not an
            # automatic second copy in the mandatory restriction response.
            return {"system_prompt_reference": {
                "kind": "event", "id": ident, "field": "system_prompt",
            }, "instructions": [
                {"message_id": row["id"], "message": row}
                for row in rows if (row["role"] == "user" or (
                    row["role"] == "system"
                    and row.get("content") != session.get("system_prompt")))
            ]}
        for row in rows:
            if type(reference["message_id"]) is int and row["id"] == reference["message_id"]:
                return {"event_id": ident, "message_id": row["id"], "message": row}
        raise ValueError("message is not part of the saved session evidence")
    field = reference.get("field")
    return {field: result[field]} if field else result
