"""Bounded continuation admission over native Kanban subscriptions and events.

The ordinary dispatcher owns recovery/advancement. A grant records a scoped
owner-verified authorization reference, never authority inferred from status.
Observations do not consume delivery cursors or create another work ledger.
"""
from __future__ import annotations

import json
import re
import time


from agent.estop import is_engaged
from hermes_constants import get_hermes_home
from hermes_cli import kanban_db as kb
from hermes_cli.profiles import profile_exists, profile_matches_home

EVENT_KINDS = ("created", "promoted", "unblocked", "status", "completed",
               "crashed", "stale", "timed_out", "reclaimed", "changes_requested",
               "review_requested", "gave_up", "blocked", "spawn_failed", "external_wait_due",
               "block_loop_detected")
PROCEDURE = "autonomous-work-continuation"


def _goal_admission_only(conn, card, event, reason, profile, job_id, *, before=None):
    """The exact goal observation receipt is not a new task effect."""
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND id > ? "
        "AND (? IS NULL OR id < ?) AND kind NOT IN ('commented', 'heartbeat') "
        "AND (? IS NOT NULL OR kind != 'continuation_reason_closed')",
        (card, event, before, before, before),
    )
    identity = {"reason": reason, "event": event, "profile": profile,
                "job_id": job_id, "profile_home": str(get_hermes_home())}
    return all((reason == "goal_closeout" and row["kind"] == "continuation_reason_admitted"
                and json.loads(row["payload"] or "{}") == identity)
               or (reason == "decision_required" and row["kind"] == "continuation_decision_effect"
                   and _decision_effect_receipt(conn, {"task_id": card, "notifier_profile": profile,
                                                       "chat_id": job_id}, event, before=before)
                   == json.loads(row["payload"] or "{}")) for row in rows)


def decision_effect_identity(sub, event, effect_event):
    """Identity written only by the scoped native CLI control handler."""
    grant = sub.get("delivery_metadata") or {}
    return {"reason": "decision_required", "event": event, "effect_event": effect_event,
            "task_id": sub["task_id"], "assignee": grant.get("authority_assignee"),
            "profile": sub["notifier_profile"], "actor": sub["notifier_profile"],
            "job_id": sub["chat_id"], "profile_home": str(get_hermes_home()),
            "authority_actor": grant.get("authority_actor"),
            "authority_reference": grant.get("authority_reference"),
            "authority_expires_at": grant.get("authority_expires_at"),
            "procedure": grant.get("procedure"), "action": "unblock", "outcome": "effect",
            "source": "host_cli"}


def _decision_effect_receipt(conn, sub, effect, *, before=None):
    current = next((s for s in kb.list_notify_subs(conn, sub["task_id"])
                    if s["platform"] == "continuation" and s["chat_id"] == sub["chat_id"]
                    and s["notifier_profile"] == sub["notifier_profile"] and not s.get("thread_id")), None)
    if current is None:
        return None
    grant = current.get("delivery_metadata") or {}
    event = grant.get("capability_decision_event")
    if (type(event) is not int or grant.get("decision_required") is not True
            or not _exact_decision_admission(conn, current, event, before=effect)
            or not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND id=? AND kind='unblocked'",
                                (sub["task_id"], effect)).fetchone()):
        return None
    identity = decision_effect_identity(current, event, effect)
    for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND id>? "
            "AND (? IS NULL OR id<?) AND kind='continuation_decision_effect'",
            (sub["task_id"], effect, before, before)):
        if json.loads(row["payload"] or "{}") == identity:
            return identity
    return None


def _verified_closure(conn, sub, reason, *, current=False, legacy_blocked_decision=False):
    """Scalar pointers alone never prove closure; require the native receipt."""
    metadata = sub.get("delivery_metadata") or {}
    event = metadata.get(f"reason_ack_event:{reason}")
    effect = metadata.get(f"reason_effect_event:{reason}")
    receipt = metadata.get(f"reason_closed_event:{reason}")
    if (any(type(value) is not int or value < 1 for value in (event, effect, receipt))
            or effect < event or receipt <= effect
            or (effect == event and reason != "goal_closeout")):
        return None
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND id = ? "
        "AND kind = 'continuation_reason_closed'", (sub["task_id"], receipt),
    ).fetchone()
    identity = {"reason": reason, "event": event, "effect_event": effect,
                "profile": sub["notifier_profile"], "job_id": sub["chat_id"],
                "profile_home": str(get_hermes_home())}
    if row is None or json.loads(row["payload"] or "{}") != identity:
        return None
    native = conn.execute("SELECT kind FROM task_events WHERE task_id = ? AND id = ?",
                          (sub["task_id"], effect)).fetchone()
    observed = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND id = ? AND kind IN ("
        + ",".join("?" for _ in EVENT_KINDS) + ")", (sub["task_id"], event, *EVENT_KINDS),
    ).fetchone()
    if (observed is None or native is None
            or (reason == "decision_required" and native["kind"] == "blocked"
                and not legacy_blocked_decision)
            or native["kind"] not in ("blocked", "completed", "review_requested", "unblocked", "changes_requested")):
        return None
    if (observed["kind"] == "block_loop_detected"
            and (reason != "decision_required"
                 or not _exact_decision_admission(conn, sub, event, before=effect)
                 or not _decision_effect_receipt(conn, sub, effect, before=receipt))):
        return None
    if legacy_blocked_decision and not (reason == "decision_required" and native["kind"] == "blocked"):
        return None  # Migration permits replacement of this old receipt only.
    if not _goal_admission_only(conn, sub["task_id"], effect, reason,
                                sub["notifier_profile"], sub["chat_id"], before=receipt):
        return None
    if current:
        task = kb.get_task(conn, sub["task_id"])
        native = conn.execute("SELECT kind FROM task_events WHERE task_id = ? AND id = ?",
                              (sub["task_id"], effect)).fetchone()
        statuses = {"blocked": "blocked", "completed": "done", "review_requested": "review",
                    "unblocked": "ready", "changes_requested": "ready"}
        if (task is None or native is None or statuses.get(native["kind"]) != task.status
                or conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id = ? AND id > ? "
                    "AND kind NOT IN ('commented', 'heartbeat') LIMIT 1",
                    (sub["task_id"], receipt),
                ).fetchone()):
            return None
    return effect


def _native_episode(conn, sub, reason, event):
    """An immutable native admission, not editable scalar episode metadata."""
    identity = {"reason": reason, "profile": sub["notifier_profile"],
                "job_id": sub["chat_id"], "profile_home": str(get_hermes_home())}
    native = conn.execute("SELECT kind FROM task_events WHERE task_id = ? AND id = ?",
                          (sub["task_id"], event)).fetchone()
    exact = native is not None and native["kind"] == "block_loop_detected"
    boundary = 0
    for row in conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id = ? "
            "AND kind = 'continuation_reason_closed' ORDER BY id", (sub["task_id"],)):
        payload = json.loads(row["payload"] or "{}")
        if (not isinstance(payload, dict) or any(payload.get(key) != value for key, value in identity.items())
                or type(payload.get("effect_event")) is not int or payload["effect_event"] >= event):
            continue
        candidate = dict(sub, delivery_metadata={
            f"reason_ack_event:{reason}": payload.get("event"),
            f"reason_effect_event:{reason}": payload["effect_event"],
            f"reason_closed_event:{reason}": row["id"],
        })
        if _verified_closure(conn, candidate, reason):
            boundary = row["id"]
    for row in conn.execute(
            "SELECT id, created_at, payload FROM task_events WHERE task_id = ? AND id > ? "
            "AND kind = 'continuation_reason_admitted' ORDER BY id", (sub["task_id"], boundary)):
        payload = json.loads(row["payload"] or "{}")
        admitted = payload.get("event") if isinstance(payload, dict) else None
        if (type(admitted) is int and boundary < admitted <= event and admitted < row["id"]
                and (not exact or admitted == event)
                and payload == {**identity, "event": admitted}
                and conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND id = ?",
                                 (sub["task_id"], admitted)).fetchone()):
            return admitted, row["created_at"]
    return None


def _triage_decision_event(conn, task, grant):
    """Resolve only the current native capability recurrence, never generic triage."""
    if (task.status != "triage" or task.block_kind != "capability"
            or task.claim_lock or task.current_run_id is not None
            or grant.get("decision_required") is not True
            or type(grant.get("capability_decision_event")) is not int
            or not kb.has_active_control_hold(conn, task.id)):
        return None
    row = conn.execute(
        "SELECT id, kind, run_id, payload FROM task_events WHERE task_id = ? "
        "AND kind NOT IN ('commented', 'heartbeat', 'attachment_added', "
        "'continuation_reason_admitted', 'continuation_reason_closed', "
        "'continuation_escalation_required') ORDER BY id DESC LIMIT 1", (task.id,),
    ).fetchone()
    run = kb.latest_run(conn, task.id)
    if (row is None or row["kind"] != "block_loop_detected"
            or row["id"] != grant["capability_decision_event"]
            or run is None or row["run_id"] != run.id
            or run.profile != task.assignee or run.outcome != "blocked"
            or run.status != "blocked" or run.ended_at is None):
        return None
    payload = kb._event_payload_dict(row)
    if (payload.get("kind") != task.block_kind or not run.summary
            or payload.get("reason") != run.summary
            or payload.get("recurrences") != task.block_recurrences
            or task.block_recurrences < kb.BLOCK_RECURRENCE_LIMIT):
        return None
    return row


def _exact_decision_admission(conn, sub, event, *, before=None):
    # The receipt must still reference the same native run/blocker. This is
    # historical validation too: an authorized effect can have ended a later run.
    native = conn.execute(
        "SELECT e.payload, r.profile, r.summary, r.status, r.outcome, r.ended_at "
        "FROM task_events e JOIN task_runs r ON r.id = e.run_id AND r.task_id = e.task_id "
        "WHERE e.task_id = ? AND e.id = ? AND e.kind = 'block_loop_detected'",
        (sub["task_id"], event),
    ).fetchone()
    task = kb.get_task(conn, sub["task_id"])
    if (native is None or task is None or native["profile"] != task.assignee
            or native["status"] != "blocked" or native["outcome"] != "blocked"
            or native["ended_at"] is None or not native["summary"]
            or kb._event_payload_dict(native).get("kind") != "capability"
            or kb._event_payload_dict(native).get("reason") != native["summary"]):
        return False
    identity = {"reason": "decision_required", "event": event,
                "profile": sub["notifier_profile"], "job_id": sub["chat_id"],
                "profile_home": str(get_hermes_home())}
    return any(json.loads(row["payload"] or "{}") == identity for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND id > ? "
        "AND (? IS NULL OR id < ?) AND kind = 'continuation_reason_admitted'",
        (sub["task_id"], event, before, before)))


def pending_decision_admission(conn, *, card: str, event: int):
    """Revalidate an immutable admission under its exact origin row and home.

    The recipient keeps its own grant/owner gates and receipts this event with
    its existing handler cursor. Receipt delivery never closes the obligation.
    """
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND id = ? "
        "AND kind = 'continuation_reason_admitted'", (card, event),
    ).fetchone()
    payload = json.loads(row["payload"] or "{}") if row else None
    if (not isinstance(payload, dict)
            or set(payload) != {"reason", "event", "profile", "job_id", "profile_home"}
            or payload["reason"] != "decision_required"
            or type(payload["event"]) is not int or not 0 < payload["event"] < event
            or not isinstance(payload["profile"], str)
            or not isinstance(payload["job_id"], str)
            or not profile_exists(payload["profile"])
            or not profile_matches_home(payload["profile"], payload["profile_home"])):
        return None
    from cron.jobs import use_cron_store
    home = get_profile_dir(payload["profile"])
    token = set_hermes_home_override(str(home))
    try:
        with use_cron_store(home):
            if not _job_permits_inference(job_id=payload["job_id"], profile=payload["profile"]):
                return None
            for sub in kb.list_notify_subs(conn, card, notifier_profiles={payload["profile"]}):
                if (sub["platform"] == "continuation" and sub["chat_id"] == payload["job_id"]
                        and not sub.get("thread_id")
                        and admission_reason(conn, sub, profile=payload["profile"]) == "decision_required"):
                    held = _triage_decision_event(conn, kb.get_task(conn, card), sub.get("delivery_metadata") or {})
                    if held and held["id"] == payload["event"]:
                        return payload
    finally:
        reset_hermes_home_override(token)
    return None


def admission_reason(conn, sub: dict, *, profile: str, profile_home=None,
                     max_in_progress: int = 1, max_per_profile: int = 1) -> str | None:
    """Return a reason only while the exact subscription grant is actionable.

    Registration is NOT approval: the installing owner must verify the
    attributable reference before registering a task/assignee-bound grant.
    The coordinator re-reads normal safety gates before any card action.
    """
    if (is_engaged() or not profile_matches_home(profile, profile_home)
            or sub.get("notifier_profile") != profile
            or type(max_in_progress) is not int or max_in_progress < 1
            or type(max_per_profile) is not int or max_per_profile < 1):
        return None
    current = next((row for row in kb.list_notify_subs(conn, sub["task_id"])
                    if row.get("platform") == sub.get("platform")
                    and row.get("chat_id") == sub.get("chat_id")
                    and (row.get("thread_id") or "") == (sub.get("thread_id") or "")
                    and row.get("notifier_profile") == profile), None)
    if current is None:
        return None
    sub = current
    grant = sub.get("delivery_metadata") or {}
    task = kb.get_task(conn, sub["task_id"])
    if task is None:
        return None
    if (grant.get("authority_actor") != "Brian"
            or not isinstance(grant.get("authority_reference"), str)
            or not grant["authority_reference"].strip()
            or type(grant.get("authority_expires_at")) is not int
            or time.time() >= grant["authority_expires_at"]
            or grant.get("authority_task_id") != task.id
            or grant.get("authority_assignee") != task.assignee
            or grant.get("procedure") != PROCEDURE):
        return None
    if (not re.fullmatch(r"t_[0-9a-f]{8}", task.id)
            or not task.assignee or not profile_exists(task.assignee)
            or not task.subscription_only or task.provider_override != "openai-codex"
            or task.model_override != "gpt-6.1-sol"
            or not kb._parents_satisfied(conn, task.id)):
        return None
    if task.status == "scheduled":
        # An overdue native wait may surface its exact owned blocker to an
        # explicitly authorized existing-context decision handler. Ordinary
        # notifications and fresh cron grants do not acquire this authority.
        if (task.claim_lock or task.current_run_id is not None
                or kb.has_active_control_hold(conn, task.id)
                or sub.get("platform") == "continuation"
                or sub.get("delivery_mode") != "wake"
                or grant.get("continuation_context") != "existing"
                or grant.get("decision_required") is not True
                or type(grant.get("external_wait_run_id")) is not int):
            return None
        run = kb.latest_run(conn, task.id)
        if (not run or run.id != grant["external_wait_run_id"]
                or run.profile != task.assignee or run.outcome != "scheduled"):
            return None
        wait = (run.metadata or {}).get("external_wait")
        try:
            kb._validate_external_wait(wait)
        except (ValueError, TypeError, AttributeError):
            return None
        due = conn.execute(
            "SELECT kind, run_id, payload FROM task_events WHERE task_id=? "
            "AND kind NOT IN ('commented', 'heartbeat', 'attachment_added') ORDER BY id DESC LIMIT 1",
            (task.id,),
        ).fetchone()
        if (wait["next_owner"] != profile or not due
                or due["kind"] != "external_wait_due" or due["run_id"] != run.id):
            return None
        payload = kb._event_payload_dict(due)
        if (any(payload.get(key) != value for key, value in wait.items())
                or payload.get("blocker") != run.summary):
            return None
        return "decision_required"  # Handler receipt only; wait/goal stays open.
    if task.status == "triage":
        if (sub.get("delivery_mode") == "wake"
                and ((sub.get("platform") == "continuation"
                      and grant.get("continuation_context") == "fresh")
                     or (sub.get("platform") != "continuation"
                         and grant.get("continuation_context") == "existing"))
                and _triage_decision_event(conn, task, grant)):
            return "decision_required"
        return None
    latest = conn.execute(
        "SELECT id, kind, payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('gave_up', 'blocked', 'block_loop_detected', 'unblocked', 'created', 'promoted_manual') "
        "ORDER BY id DESC LIMIT 1", (task.id,),
    ).fetchone()
    # Surface a needs_input hold or an explicitly event-bound capability
    # decision. Neither grants execution or releases the real operator hold.
    if (task.status == "blocked" and not task.claim_lock
            and latest and latest["kind"] == "blocked"
            and json.loads(latest["payload"] or "{}").get("kind") == task.block_kind
            and (task.block_kind == "needs_input"
                 or (task.block_kind == "capability"
                     and grant.get("decision_required") is True
                     and type(grant.get("capability_decision_event")) is int
                     and grant["capability_decision_event"] == latest["id"]))
            and sub.get("delivery_mode") == "wake"
            and ((sub.get("platform") != "continuation"
                  and grant.get("continuation_context") == "existing")
                 or (sub.get("platform") == "continuation"
                     and grant.get("continuation_context") == "fresh"
                     and grant.get("decision_required") is True))):
        return "decision_required"
    if kb.has_active_control_hold(conn, task.id):
        return None
    if task.status == "done":
        # Owner-declared goal identity, never ordinary leaf completion.
        children = conn.execute(
            "SELECT status FROM tasks WHERE id IN "
            "(SELECT child_id FROM task_links WHERE parent_id = ?)", (task.id,),
        ).fetchall()
        return "goal_closeout" if (grant.get("goal_closeout") is True
            and grant.get("goal_task_id") == task.id and children
            and all(row["status"] == "done" for row in children)) else None
    if task.status == "running" or task.claim_lock:
        return None
    if task.status == "blocked" and latest and latest["kind"] == "gave_up":
        return "retry_exhausted"
    # Healthy native advancement/review/recovery requires no coordinator turn.
    if task.status not in ("ready", "review"):
        return None
    if kb.check_respawn_guard(conn, task.id, lane=task.status) != "blocker_auth":
        return None
    running = kb.count_running_tasks(conn) + kb.count_running_tasks_other_boards()
    owned = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'running' AND assignee = ?",
        (task.assignee,),
    ).fetchone()[0]
    if (running >= max_in_progress or owned >= max_per_profile
            or kb._memory_pressure_level() == "critical"):
        return None
    return "authentication_blocker"


def collect_wakeup(conn, *, job_id: str, profile: str,
                   max_in_progress: int = 1, max_per_profile: int = 1) -> dict:
    """Observe one scoped cron transition; native monitor/claim gates own dedup.

    A failed/interrupted observation never advances the Kanban cursor. After
    a verified action/closeout the coordinator acknowledges the exact event.
    Running cards are exclusively owned by the ordinary dispatcher.
    """
    if not job_id:
        return {"wakeAgent": False}
    exceptions = []
    with kb.write_txn(conn):
        for sub in kb.list_notify_subs(conn, notifier_profiles={profile}):
            if (sub.get("platform") != "continuation" or sub.get("chat_id") != job_id
                    or sub.get("thread_id")):
                continue
            reason = admission_reason(conn, sub, profile=profile,
                                      max_in_progress=max_in_progress,
                                      max_per_profile=max_per_profile)
            if reason is None:
                continue
            cursor, events = kb.unseen_events_for_sub(
                conn, task_id=sub["task_id"], platform="continuation", chat_id=job_id,
                kinds=EVENT_KINDS,
            )
            if reason == "decision_required":
                # Owner binding may postdate the real hold. Observe its native
                # identity without rewinding the installation/delivery cursor.
                held = conn.execute(
                    "SELECT id FROM task_events WHERE task_id = ? AND kind IN ('blocked', 'block_loop_detected') "
                    "ORDER BY id DESC LIMIT 1", (sub["task_id"],),
                ).fetchone()
                cursor, events = (held["id"], [held]) if held else (cursor, [])
            boundary = _verified_closure(conn, sub, reason) or 0
            if events and cursor > boundary:
                metadata = dict(sub.get("delivery_metadata") or {})
                # Native subscription metadata preserves scalar values only.
                observation_key = f"reason_observed_event:{reason}"
                triage = reason == "decision_required" and conn.execute(
                    "SELECT 1 FROM task_events WHERE id = ? AND kind = 'block_loop_detected'",
                    (cursor,),
                ).fetchone() is not None
                if (metadata.get(observation_key) != cursor
                        or (triage and not _exact_decision_admission(conn, sub, cursor))):
                    # Admission starts one reason episode, not one poll. Native
                    # liveness or repeated delivery cannot extend its deadline.
                    previous = metadata.get(observation_key, 0)
                    if (not previous or previous <= boundary
                            or f"reason_admitted_at:{reason}" not in metadata):
                        metadata[f"reason_admitted_at:{reason}"] = int(time.time())
                        metadata[f"reason_episode_event:{reason}"] = cursor
                    if ((triage and not _exact_decision_admission(conn, sub, cursor))
                            or (not triage and _native_episode(conn, sub, reason, cursor) is None)):
                        kb._append_event(conn, sub["task_id"], "continuation_reason_admitted", {
                            "reason": reason, "event": cursor, "profile": profile,
                            "job_id": job_id, "profile_home": str(get_hermes_home()),
                        })
                    metadata[observation_key] = cursor
                    conn.execute(
                        "UPDATE kanban_notify_subs SET delivery_metadata = ? WHERE task_id = ? "
                        "AND platform = 'continuation' AND chat_id = ? AND thread_id = '' "
                        "AND notifier_profile = ?",
                        (json.dumps(metadata, sort_keys=True), sub["task_id"], job_id, profile),
                    )
                exceptions.append({"card": sub["task_id"], "reason": reason, "event": cursor})
                if len(exceptions) == 8:
                    break
    if exceptions:
        result = {"wakeAgent": True, **exceptions[0], "procedure": PROCEDURE}
        if len(exceptions) > 1:
            result["exceptions"] = exceptions
        return result
    return {"wakeAgent": False}


def observe_pending_obligations(conn, *, job_id: str, profile: str,
                                deadline_seconds: int, now: float | None = None) -> list[dict]:
    """Read admitted native reasons without consuming, recovering or inferring.

    The caller supplies an explicit semantic deadline policy; scheduler stale
    thresholds are NOT such a policy. It starts at first reason admission and
    cannot be extended by polling, heartbeat, commentary or model/delivery
    success. Legacy observations lack a recorded admission time and conservatively
    use their native event time. A pending episode has no verified progress:
    current validated effect acknowledgement closes it rather than renewing it.

    admission_actionable is only the native admission predicate, NOT execution
    permission. Escalation requires exact job/inference/owner/stop/hold gates.
    This reader calls no model; automatic recovery is not supported in the MVP.
    Ordinary running work stays dispatcher-owned.
    """
    if type(deadline_seconds) is not int or deadline_seconds < 1:
        raise ValueError("deadline_seconds must be an explicit positive integer")
    if not job_id or not profile_matches_home(profile):
        return []
    now = time.time() if now is None else now
    items = []
    for sub in kb.list_notify_subs(conn, notifier_profiles={profile}):
        if (sub.get("platform") != "continuation" or sub.get("chat_id") != job_id
                or sub.get("thread_id")):
            continue
        task = kb.get_task(conn, sub["task_id"])
        if task is None or task.status == "running" or task.claim_lock:
            continue
        metadata = sub.get("delivery_metadata") or {}
        for reason in ("authentication_blocker", "retry_exhausted", "goal_closeout", "decision_required"):
            event = metadata.get(f"reason_observed_event:{reason}")
            if type(event) is not int or event < 1:
                continue
            native = conn.execute(
                "SELECT kind, created_at FROM task_events WHERE task_id = ? AND id = ? "
                "AND kind IN (" + ",".join("?" for _ in EVENT_KINDS) + ")",
                (task.id, event, *EVENT_KINDS),
            ).fetchone()
            if native is None:
                continue
            if (metadata.get(f"reason_ack_event:{reason}") == event
                    and _verified_closure(conn, sub, reason, current=True)):
                continue
            episode = _native_episode(conn, sub, reason, event)
            admitted_at = episode[1] if episode else native["created_at"]
            deadline_at = admitted_at + deadline_seconds
            actionable = admission_reason(conn, sub, profile=profile) == reason
            if native["kind"] == "block_loop_detected":
                actionable = (actionable and metadata.get("capability_decision_event") == event
                              and _exact_decision_admission(conn, sub, event))
            items.append({
                "card": task.id, "reason": reason, "event": event,
                "episode_event": episode[0] if episode else None,
                "profile": profile, "profile_home": str(get_hermes_home()),
                "job_id": job_id, "admitted_at": admitted_at,
                "admission_age_seconds": max(0, now - admitted_at),
                "last_verified_progress_at": None, "deadline_at": deadline_at,
                "overdue": now >= deadline_at,
                "admission_actionable": actionable,
            })
    return items


def acknowledge_wakeup(conn, *, job_id: str, profile: str, card: str, event: int,
                       reason: str | None = None, effect_event: int | None = None) -> bool:
    """Acknowledge a reason only with a current native task-effect receipt.

    A successful model turn, comment or heartbeat is not a task effect. Keep
    the subscription baseline intact so another reason remains admissible.
    """
    if (not profile_matches_home(profile) or type(event) is not int or event < 1
            or reason not in ("authentication_blocker", "retry_exhausted", "goal_closeout", "decision_required")
            or type(effect_event) is not int or effect_event < event
            or (effect_event == event and reason != "goal_closeout")):
        return False
    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT last_event_id, delivery_metadata FROM kanban_notify_subs WHERE task_id = ? "
            "AND platform = 'continuation' AND chat_id = ? AND thread_id = '' "
            "AND notifier_profile = ?", (card, job_id, profile),
        ).fetchone()
        exists = conn.execute(
            "SELECT kind FROM task_events WHERE id = ? AND task_id = ? "
            "AND kind IN (" + ",".join("?" for _ in EVENT_KINDS) + ")",
            (event, card, *EVENT_KINDS),
        ).fetchone()
        if (row is None or exists is None
                or (event < row["last_event_id"] and reason != "decision_required")):
            return False
        effect = conn.execute(
            "SELECT kind FROM task_events WHERE id = ? AND task_id = ?",
            (effect_event, card),
        ).fetchone()
        task = kb.get_task(conn, card)
        statuses = {"blocked": "blocked", "completed": "done", "review_requested": "review",
                    "unblocked": "ready", "changes_requested": "ready"}
        if effect is None or task is None or statuses.get(effect["kind"]) != task.status:
            return False
        if reason == "decision_required" and effect["kind"] == "blocked":
            return False  # A new unresolved hold is not decision follow-through.
        # Returning to the same status does not revive a superseded receipt.
        # Commentary/liveness alone neither verifies nor invalidates an effect.
        if not _goal_admission_only(conn, card, effect_event, reason, profile, job_id):
            return False
        metadata = json.loads(row["delivery_metadata"] or "{}")
        if (metadata.get("authority_actor") != "Brian"
                or metadata.get("procedure") != PROCEDURE
                or not isinstance(metadata.get("authority_reference"), str)
                or not metadata["authority_reference"].strip()
                or metadata.get("authority_task_id") != card
                or metadata.get("authority_assignee") != task.assignee
                or type(metadata.get("authority_expires_at")) is not int
                or time.time() >= metadata["authority_expires_at"]):
            return False
        if metadata.get(f"reason_observed_event:{reason}") != event:
            return False
        if exists["kind"] == "block_loop_detected":
            sub = {"task_id": card, "notifier_profile": profile, "chat_id": job_id}
            if (reason != "decision_required" or metadata.get("decision_required") is not True
                    or metadata.get("capability_decision_event") != event
                    or not _exact_decision_admission(conn, sub, event, before=effect_event)
                    or not _decision_effect_receipt(conn, sub, effect_event)
                    or conn.execute(
                        "SELECT 1 FROM task_events WHERE task_id = ? AND id > ? AND id < ? "
                        "AND kind IN ('blocked', 'block_loop_detected') LIMIT 1",
                        (card, event, effect_event),
                    ).fetchone()):
                return False
        if reason == "goal_closeout":
            sub = {"task_id": card, "platform": "continuation", "chat_id": job_id,
                   "thread_id": "", "notifier_profile": profile}
            if (effect["kind"] != "completed"
                    or admission_reason(conn, sub, profile=profile) != "goal_closeout"):
                return False
        ack_key = f"reason_ack_event:{reason}"
        effect_key = f"reason_effect_event:{reason}"
        previous = metadata.get(ack_key)
        if previous and event <= previous:
            sub = {"task_id": card, "notifier_profile": profile, "chat_id": job_id,
                   "delivery_metadata": metadata}
            if not (previous == event and _verified_closure(
                    conn, sub, reason, legacy_blocked_decision=True)):
                return (previous == event and metadata.get(effect_key) == effect_event
                        and _verified_closure(conn, sub, reason, current=True) == effect_event)
        metadata[ack_key] = event
        metadata[effect_key] = effect_event
        kb._append_event(conn, card, "continuation_reason_closed", {
            "reason": reason, "event": event, "effect_event": effect_event,
            "profile": profile, "job_id": job_id, "profile_home": str(get_hermes_home()),
        })
        metadata[f"reason_closed_event:{reason}"] = conn.execute(
            "SELECT MAX(id) FROM task_events WHERE task_id = ?", (card,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE kanban_notify_subs SET delivery_metadata = ? WHERE task_id = ? "
            "AND platform = 'continuation' AND chat_id = ? AND thread_id = '' "
            "AND notifier_profile = ?", (json.dumps(metadata, sort_keys=True), card, job_id, profile),
        )
    return True


def _job_permits_inference(*, job_id: str, profile: str) -> bool:
    """Re-read the existing exact profile/job/inference admission contract."""
    from cron.jobs import get_job, is_job_runnable

    job = get_job(job_id) if profile_matches_home(profile) else None
    if (job is None or not is_job_runnable(job)
            or job.get("subscription_only") is not True
            or job.get("provider") != "openai-codex"
            or job.get("model") != "gpt-6.1-sol"
            or job.get("base_url")
            or job.get("attach_to_session") is not False
            or job.get("continuity") or job.get("context_from")
            or job.get("skills") or job.get("prompt_path")
            or not isinstance(job.get("prompt"), str) or len(job["prompt"]) > 200
            or not job.get("script") or job.get("monitor_script") != job.get("script")):
        return False
    return True


def signal_pending_escalation(conn, *, job_id: str, profile: str,
                              card: str, reason: str, deadline_seconds: int,
                              coordinator_state: str, owner_gate,
                              now: float | None = None) -> dict | None:
    """Record one actionable missed-progress signal, never replay an action.

    The existing owner/decision route consumes the returned observation. This
    helper does not send messages, launch inference, unblock or acknowledge.
    owner_gate rechecks reservation, stop, cancel, revocation and incident policy
    and returns literal True. Native admission and exact job gates also bind.
    Uncertain/crashed former recovery attempts are conservatively escalated.
    """
    if coordinator_state not in ("unavailable", "dead", "stale"):
        return None
    with kb.write_txn(conn):
        # Freeze age/deadline observation only; admission still checks live time.
        now = time.time() if now is None else now
        items = observe_pending_obligations(
            conn, job_id=job_id, profile=profile, deadline_seconds=deadline_seconds, now=now)
        item = next((item for item in items if item["card"] == card and item["reason"] == reason), None)
        if (item is None or item["episode_event"] is None or not item["overdue"]
                or not item["admission_actionable"]
                or not _job_permits_inference(job_id=job_id, profile=profile)
                or owner_gate(dict(item)) is not True):
            return None
        # Re-read after the owner gate; its result cannot revive a stale admission.
        current = observe_pending_obligations(
            conn, job_id=job_id, profile=profile, deadline_seconds=deadline_seconds, now=now)
        if item not in current or not _job_permits_inference(job_id=job_id, profile=profile):
            return None
        identity = {key: item[key] for key in
                    ("reason", "profile", "profile_home", "job_id", "episode_event")}
        for row in conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? "
                "AND kind = 'continuation_escalation_required'", (card,)):
            if json.loads(row["payload"] or "{}") == identity:
                return None
        kb._append_event(conn, card, "continuation_escalation_required", identity)
    return {**item, "action": "owner_decision_required"}


def pending_escalation(conn, *, card: str, event: int) -> dict | None:
    """Reread a native signal under its origin's exact profile/job gates.

    Delivery uses the existing recipient subscription/handler cursor, not the
    producer's return value. A handler receipt is NOT obligation closure.
    """
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from cron.jobs import use_cron_store

    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND id = ? "
        "AND kind = 'continuation_escalation_required'", (card, event),
    ).fetchone()
    payload = json.loads(row["payload"] or "{}") if row else None
    if (not isinstance(payload, dict)
            or set(payload) != {"reason", "profile", "profile_home", "job_id", "episode_event"}
            or not isinstance(payload["profile"], str)
            or not isinstance(payload["job_id"], str)
            or not profile_exists(payload["profile"])
            or not profile_matches_home(payload["profile"], payload["profile_home"])
            or type(payload["episode_event"]) is not int
            or not 0 < payload["episode_event"] < event):
        return None
    home = get_profile_dir(payload["profile"])
    token = set_hermes_home_override(str(home))
    try:
        with use_cron_store(home):
            if not _job_permits_inference(job_id=payload["job_id"], profile=payload["profile"]):
                return None
            items = observe_pending_obligations(
                conn, job_id=payload["job_id"], profile=payload["profile"], deadline_seconds=1)
            if any(item["card"] == card and item["reason"] == payload["reason"]
                   and item["episode_event"] == payload["episode_event"]
                   and item["admission_actionable"] for item in items):
                return payload
    finally:
        reset_hermes_home_override(token)
    return None


def emit_gate(*, job_id: str, profile: str) -> None:
    """Supported script adapter; fail closed before any coordinator inference."""
    if not _job_permits_inference(job_id=job_id, profile=profile):
        print(json.dumps({"wakeAgent": False}))
        return
    with kb.connect_closing() as conn:
        print(json.dumps(collect_wakeup(conn, job_id=job_id, profile=profile), sort_keys=True))
