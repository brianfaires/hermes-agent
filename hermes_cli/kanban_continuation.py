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
               "review_requested", "gave_up", "blocked", "spawn_failed", "external_wait_due")
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
    return all(reason == "goal_closeout" and row["kind"] == "continuation_reason_admitted"
               and json.loads(row["payload"] or "{}") == identity for row in rows)


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
        "SELECT 1 FROM task_events WHERE task_id = ? AND id = ? AND kind IN ("
        + ",".join("?" for _ in EVENT_KINDS) + ")", (sub["task_id"], event, *EVENT_KINDS),
    ).fetchone()
    if (observed is None or native is None
            or (reason == "decision_required" and native["kind"] == "blocked"
                and not legacy_blocked_decision)
            or native["kind"] not in ("blocked", "completed", "review_requested", "unblocked", "changes_requested")):
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
    if reason == "next_selection":
        for row in conn.execute(
                "SELECT created_at,payload FROM task_events WHERE task_id=? "
                "AND kind='continuation_reason_admitted' ORDER BY id", (sub['task_id'],)):
            if json.loads(row['payload']) == {**identity, "event": event}:
                return event, row['created_at']
        return None
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
                and payload == {**identity, "event": admitted}
                and conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND id = ?",
                                 (sub["task_id"], admitted)).fetchone()):
            return admitted, row["created_at"]
    return None


def register_standing_coordination(conn, *, card, profile, job_id,
                                   authority_actor, authority_reference):
    """Owner registration of one selected card; never upgrade an existing grant.

    The caller verifies attribution and bounded scope before calling. This is
    coordination permission only, not permission to execute or release holds.
    """
    if (authority_actor != "Brian" or not isinstance(authority_reference, str)
            or not authority_reference.strip() or not profile_matches_home(profile)
            or not job_id):
        raise ValueError("exact attributable owner/home/job required")
    with kb.write_txn(conn):
        task = kb.get_task(conn, card)
        if task is None or not task.assignee or not profile_exists(task.assignee):
            raise ValueError("existing assigned card required")
        subscriptions = kb.list_notify_subs(conn, card)
        if any('authority_expires_at' in (s.get('delivery_metadata') or {})
               or (s['platform'] == 'continuation' and s['chat_id'] == job_id)
               for s in subscriptions):
            raise ValueError("existing subscription must not be upgraded or renewed")
        metadata = {
            "authority_actor": authority_actor, "authority_reference": authority_reference,
            "authority_mode": "standing_coordination", "authority_task_id": card,
            "authority_assignee": task.assignee, "authority_home": str(get_hermes_home()),
            "authority_job_id": job_id, "authority_paused": False, "authority_revoked": False,
            "procedure": PROCEDURE}
        conn.execute(
            "INSERT INTO kanban_notify_subs "
            "(task_id,platform,chat_id,thread_id,notifier_profile,delivery_mode,"
            "delivery_metadata,created_at,last_event_id) VALUES (?, 'continuation', ?, '', ?, "
            "'wake', ?, ?, COALESCE((SELECT MAX(id) FROM task_events WHERE task_id=?),0))",
            (card, job_id, profile, json.dumps(metadata, sort_keys=True), int(time.time()), card))


def _standing_valid(sub, task):
    from cron.jobs import get_job, is_job_runnable
    m = sub.get('delivery_metadata') or {}
    job = get_job(sub['chat_id'])
    return (m.get('authority_mode') == 'standing_coordination'
            and 'authority_expires_at' not in m
            and m.get('authority_actor') == 'Brian'
            and isinstance(m.get('authority_reference'), str) and bool(m['authority_reference'].strip())
            and m.get('authority_task_id') == task.id
            and m.get('authority_assignee') == task.assignee
            and m.get('authority_home') == str(get_hermes_home())
            and m.get('authority_job_id') == sub['chat_id']
            and m.get('authority_paused') is False and m.get('authority_revoked') is False
            and m.get('procedure') == PROCEDURE
            and sub.get('platform') == 'continuation' and not sub.get('thread_id')
            and sub.get('delivery_mode') == 'wake'
            and job is not None and is_job_runnable(job)
            and job.get('attach_to_session') is False
            and bool(job.get('script')) and job.get('monitor_script') == job.get('script')
            and not job.get('continuity') and not job.get('context_from'))


def _selection_trigger(conn, card):
    task = kb.get_task(conn, card)
    if task is None or task.claim_lock or task.current_run_id is not None:
        return None
    kind = {'done': 'completed', 'blocked': 'blocked'}.get(task.status)
    if kind is None:
        return None
    row = conn.execute("SELECT id, kind FROM task_events WHERE task_id=? "
                       "AND kind IN ('completed','blocked','gave_up','unblocked','created','status') "
                       "ORDER BY id DESC LIMIT 1", (card,)).fetchone()
    return row['id'] if row and (row['kind'] == kind or
                               (task.status == 'blocked' and row['kind'] == 'gave_up')) else None


def _selection_identity(sub, event):
    return dict(event=event, profile=sub['notifier_profile'], job_id=sub['chat_id'],
                profile_home=str(get_hermes_home()))


def _selection_receipt(conn, sub, event):
    identity = _selection_identity(sub, event)
    for row in conn.execute("SELECT payload FROM task_events WHERE task_id=? "
                            "AND kind='continuation_selection_closed'", (sub['task_id'],)):
        p = json.loads(row['payload'])
        if all(p.get(k) == v for k, v in identity.items()):
            return p
    return None


def _selection_sub(conn, card, profile, job_id):
    return next((s for s in kb.list_notify_subs(conn, card)
                 if s['platform'] == 'continuation' and s['chat_id'] == job_id
                 and not s.get('thread_id') and s['notifier_profile'] == profile), None)


def bind_next_selection(conn, *, card, event, profile, job_id, next_card,
                        max_in_progress=1, max_per_profile=1):
    """Record Rook's exact choice before ordinary authorized dispatch; never launch."""
    with kb.write_txn(conn):
        sub = _selection_sub(conn, card, profile, job_id)
        if (sub is None or _selection_trigger(conn, card) != event
                or admission_reason(conn, sub, profile=profile,
                                    max_in_progress=max_in_progress,
                                    max_per_profile=max_per_profile) != 'next_selection'):
            return False
        target = kb.get_task(conn, next_card)
        if (target is None or next_card == card or target.status != 'ready'
                or target.claim_lock or target.current_run_id is not None
                or not target.assignee or not profile_exists(target.assignee)
                or kb.has_active_control_hold(conn, next_card)
                or not kb._parents_satisfied(conn, next_card)
                or kb.check_respawn_guard(conn, next_card, lane='ready') is not None
                or conn.execute("SELECT COUNT(*) FROM tasks WHERE status='running' AND assignee=?",
                                (target.assignee,)).fetchone()[0] >= max_per_profile):
            return False
        target_sub = _selection_sub(conn, next_card, profile, job_id)
        if target_sub is None or not _standing_valid(target_sub, target):
            return False
        identity = _selection_identity(sub, event)
        # A selected target is reserved by one unresolved native binding. A
        # restart or another source completion must not dispatch it twice.
        for row in conn.execute("SELECT task_id,payload FROM task_events "
                                "WHERE kind='continuation_selection_bound'"):
            other = json.loads(row['payload'])
            if other.get('next_card') != next_card or (
                    row['task_id'] == card and all(other.get(k) == v for k, v in identity.items())):
                continue
            closed = any(
                all(json.loads(r['payload']).get(k) == other.get(k)
                    for k in ('event', 'profile', 'job_id', 'profile_home', 'next_card'))
                for r in conn.execute("SELECT payload FROM task_events WHERE task_id=? "
                                      "AND kind='continuation_selection_closed'", (row['task_id'],)))
            if not closed:
                return False
        previous = _selection_binding(conn, card, identity)
        if previous:
            return previous[1]['next_card'] == next_card
        kb._append_event(conn, card, 'continuation_selection_bound', {
            **identity, 'next_card': next_card, 'assignee': target.assignee,
            'model': target.model_override, 'provider': target.provider_override})
        return True


def _selection_binding(conn, card, identity):
    for row in conn.execute("SELECT id,payload FROM task_events WHERE task_id=? "
                            "AND kind='continuation_selection_bound' ORDER BY id", (card,)):
        p = json.loads(row['payload'])
        if all(p.get(k) == v for k, v in identity.items()):
            return row['id'], p
    return None


def acknowledge_next_selection(conn, *, card, event, profile, job_id, next_card, effect_event):
    """Close only with the selected card's native spawned run, never model success."""
    with kb.write_txn(conn):
        sub = _selection_sub(conn, card, profile, job_id)
        source = kb.get_task(conn, card)
        if (sub is None or source is None or is_engaged() or not profile_matches_home(profile)
                or not _standing_valid(sub, source) or _selection_trigger(conn, card) != event):
            return False
        bound = _selection_binding(conn, card, _selection_identity(sub, event))
        if not bound or type(effect_event) is not int or effect_event <= bound[0]:
            return False
        p = bound[1]
        target = kb.get_task(conn, next_card)
        target_sub = _selection_sub(conn, next_card, profile, job_id)
        if target is None or target_sub is None or not _standing_valid(target_sub, target):
            return False
        receipt = _selection_receipt(conn, sub, event)
        if receipt:
            return receipt['next_card'] == next_card and receipt['effect_event'] == effect_event
        native = conn.execute("SELECT run_id,payload FROM task_events WHERE id=? AND task_id=? "
                              "AND kind='spawned'", (effect_event, next_card)).fetchone()
        run = kb.get_run(conn, native['run_id']) if native and native['run_id'] else None
        spawn_pid = json.loads(native['payload']).get('pid') if native else None
        if (p['next_card'] != next_card or target is None or run is None
                or run.task_id != next_card or run.profile != p['assignee']
                or target.assignee != p['assignee'] or target.model_override != p['model']
                or target.provider_override != p['provider']
                or not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? "
                                    "AND kind='claimed' AND id>? AND id<?",
                                    (next_card, run.id, bound[0], effect_event)).fetchone()
                or type(spawn_pid) is not int or spawn_pid < 1):
            return False
        # _end_run clears worker_pid. The immutable claim/spawn pair above
        # proves launch even if the worker finished before the first ack.
        kb._append_event(conn, card, 'continuation_selection_closed', {
            **_selection_identity(sub, event), 'next_card': next_card,
            'effect_event': effect_event, 'run_id': run.id})
        return True


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
    if grant.get("authority_mode") == "standing_coordination":
        if (not _standing_valid(sub, task) or not kb._parents_satisfied(conn, task.id)
                or not task.assignee or not profile_exists(task.assignee)):
            return None
        event = _selection_trigger(conn, task.id)
        if not event or _selection_receipt(conn, sub, event):
            return None
        if (kb.count_running_tasks(conn) + kb.count_running_tasks_other_boards() >= max_in_progress
                or kb._memory_pressure_level() == "critical"):
            return None
        return "next_selection"
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
            or task.provider_override != "openai-codex"
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
    latest = conn.execute(
        "SELECT id, kind, payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('gave_up', 'blocked', 'unblocked', 'created', 'promoted_manual') "
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
                   max_in_progress: int = 1, max_per_profile: int = 1,
                   standing_only: bool = False) -> dict:
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
            # The script adapter may admit standing coordination under user
            # pins without granting that job access to legacy obligations.
            if standing_only and (sub.get("delivery_metadata") or {}).get(
                    "authority_mode") != "standing_coordination":
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
            if reason == "next_selection":
                cursor = _selection_trigger(conn, sub["task_id"])
                events = [{"id": cursor}] if cursor else []
            if reason == "decision_required":
                # Owner binding may postdate the real hold. Observe its native
                # identity without rewinding the installation/delivery cursor.
                held = conn.execute(
                    "SELECT id FROM task_events WHERE task_id = ? AND kind = 'blocked' "
                    "ORDER BY id DESC LIMIT 1", (sub["task_id"],),
                ).fetchone()
                cursor, events = (held["id"], [held]) if held else (cursor, [])
            boundary = _verified_closure(conn, sub, reason) or 0
            if events and cursor > boundary:
                metadata = dict(sub.get("delivery_metadata") or {})
                # Native subscription metadata preserves scalar values only.
                observation_key = f"reason_observed_event:{reason}"
                if metadata.get(observation_key) != cursor:
                    # Admission starts one reason episode, not one poll. Native
                    # liveness or repeated delivery cannot extend its deadline.
                    previous = metadata.get(observation_key, 0)
                    if (not previous or previous <= boundary
                            or f"reason_admitted_at:{reason}" not in metadata):
                        metadata[f"reason_admitted_at:{reason}"] = int(time.time())
                        metadata[f"reason_episode_event:{reason}"] = cursor
                    if _native_episode(conn, sub, reason, cursor) is None:
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
        for reason in ("authentication_blocker", "retry_exhausted", "goal_closeout", "decision_required", "next_selection"):
            event = metadata.get(f"reason_observed_event:{reason}")
            if type(event) is not int or event < 1:
                continue
            native = conn.execute(
                "SELECT created_at FROM task_events WHERE task_id = ? AND id = ? "
                "AND kind IN (" + ",".join("?" for _ in EVENT_KINDS) + ")",
                (task.id, event, *EVENT_KINDS),
            ).fetchone()
            if native is None:
                continue
            if reason == "next_selection" and _selection_receipt(conn, sub, event):
                continue
            if (metadata.get(f"reason_ack_event:{reason}") == event
                    and _verified_closure(conn, sub, reason, current=True)):
                continue
            episode = _native_episode(conn, sub, reason, event)
            admitted_at = episode[1] if episode else native["created_at"]
            deadline_at = admitted_at + deadline_seconds
            items.append({
                "card": task.id, "reason": reason, "event": event,
                "episode_event": episode[0] if episode else None,
                "profile": profile, "profile_home": str(get_hermes_home()),
                "job_id": job_id, "admitted_at": admitted_at,
                "admission_age_seconds": max(0, now - admitted_at),
                "last_verified_progress_at": None, "deadline_at": deadline_at,
                "overdue": now >= deadline_at,
                "admission_actionable": admission_reason(conn, sub, profile=profile) == reason,
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
            "SELECT 1 FROM task_events WHERE id = ? AND task_id = ? "
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
    legacy_allowed = _job_permits_inference(job_id=job_id, profile=profile)
    with kb.connect_closing() as conn:
        print(json.dumps(collect_wakeup(
            conn, job_id=job_id, profile=profile, standing_only=not legacy_allowed,
        ), sort_keys=True))
