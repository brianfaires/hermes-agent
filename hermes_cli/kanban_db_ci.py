"""Single-claim CI continuation proof; ordinary unblock is deliberately not a PR handoff."""
from __future__ import annotations

import re
import time


_BINDING_KEYS = {"repo", "ci_run", "pr", "head_sha"}
_CONCLUSIONS = {"success", "failure", "cancelled", "timed_out", "action_required", "neutral", "skipped", "stale", "startup_failure"}


def _binding(value):
    if (not isinstance(value, dict) or set(value) != _BINDING_KEYS
            or not isinstance(value["repo"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repo"])
            or any(type(value[k]) is not int or value[k] <= 0 for k in ("ci_run", "pr"))
            or not isinstance(value["head_sha"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", value["head_sha"])):
        raise ValueError("CI wait requires exact repository, positive run/PR and full head SHA")
    return dict(value)


def _result(value, binding):
    if not isinstance(value, dict) or set(value) != _BINDING_KEYS | {"status", "conclusion"}:
        raise ValueError("CI result is incomplete")
    if (_binding({k: value[k] for k in _BINDING_KEYS}) != binding
            or value["status"] != "completed" or not isinstance(value["conclusion"], str)
            or value["conclusion"] not in _CONCLUSIONS):
        raise ValueError("CI result does not match the exact terminal wait")
    return dict(value)


def _run_matches(conn, task, run_id):
    run = conn.execute("SELECT id, profile, outcome FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                       (task.id,)).fetchone()
    return bool(run and run["id"] == run_id and run["profile"] == task.assignee
                and run["outcome"] == "scheduled")


def park_binding(conn, task_id, run_id, value):
    """Called inside schedule_task's transaction before any state change."""
    from hermes_cli import kanban_db as kb
    binding = _binding(value)
    task = kb.get_task(conn, task_id)
    run = conn.execute("SELECT task_id, profile FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    if (not task or task.status != "running" or task.current_run_id != run_id
            or task.goal_mode or kb._has_sticky_block(conn, task_id) or not task.assignee
            or not run or run["task_id"] != task_id or run["profile"] != task.assignee):
        raise ValueError("CI wait must belong to this unheld running owner attempt")
    comment_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_comments WHERE task_id = ?", (task_id,)).fetchone()[0]
    return {"binding": binding, "task": task_id, "run": run_id, "owner": task.assignee, "comment_id": comment_id}


def _parked_worker_finished(conn, task_id, event_id, run_id, row):
    from hermes_cli import kanban_db as kb
    if row["run_id"] != run_id:
        return False
    bound = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? "
        "AND kind = 'worker_bound' AND id < ? ORDER BY id DESC LIMIT 1",
        (task_id, run_id, event_id)).fetchone()
    identity = kb._json_dict(bound["payload"]) if bound else {}
    result = kb._json_dict(row["payload"])
    return (type(result.get("exit_code")) is int and result["exit_code"] == 0
            and type(result.get("pid")) is int and result["pid"] > 0
            and isinstance(result.get("claim_lock"), str) and bool(result["claim_lock"])
            and type(identity.get("pid")) is int
            and (result["pid"], result["claim_lock"]) == (identity["pid"], identity.get("claim_lock")))


def _unchanged_since_park(conn, task_id, event_id, run_id):
    # Event IDs catch same-second hold/owner round trips. Only the parked
    # managed worker's successful finalizer is benign; unknown events still deny.
    for row in conn.execute("SELECT kind, run_id, payload FROM task_events WHERE task_id = ? AND id > ?",
                            (task_id, event_id)):
        if row["kind"] in {"commented", "unblocked", "respawn_guarded", "promoted", "linked"}:
            continue
        if row["kind"] != "worker_result" or not _parked_worker_finished(conn, task_id, event_id, run_id, row):
            return False
    return True


def _event(conn, task_id, kind):
    return conn.execute("SELECT id, run_id, payload, created_at FROM task_events "
                        "WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1", (task_id, kind)).fetchone()


def _resume_wait(conn, task_id, run_id):
    from hermes_cli import kanban_db as kb
    event = _event(conn, task_id, "scheduled")
    wait = kb._json_dict(event["payload"]).get("ci_wait") if event else None
    task = kb.get_task(conn, task_id)
    if (not isinstance(wait, dict) or not task or task.status != "scheduled"
            or wait.get("task") != task_id or wait.get("run") != run_id
            or event["run_id"] != run_id or wait.get("owner") != task.assignee
            or not _run_matches(conn, task, run_id) or not _unchanged_since_park(conn, task_id, event["id"], run_id)):
        raise ValueError("CI result belongs to a stale wait or changed owner/lifecycle")
    return event, wait


def matches_ci_wait(conn, task_id, run_id, value):
    """Error results retain the original owner fence without granting a continuation."""
    try:
        _, wait = _resume_wait(conn, task_id, run_id)
        return _binding(wait["binding"]) == _binding(value)
    except (KeyError, TypeError, ValueError):
        return False


def resume_binding(conn, task_id, run_id, value):
    """Called inside unblock_task's existing fenced transaction; no new mutation path."""
    event, wait = _resume_wait(conn, task_id, run_id)
    result = _result(value, _binding(wait.get("binding")))
    return {"scheduled_event_id": event["id"], "wait": wait, "result": result}


def qualifies_ci_resume(conn, task_id, pr_comments):
    """Lift only active_pr, only until the next claim; no network or dispatch side effects."""
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
    task = kb.get_task(conn, task_id)
    event = _event(conn, task_id, "unblocked")
    proof = kb._json_dict(event["payload"]).get("ci_resume") if event else None
    if (not task or task.status != "ready" or task.current_run_id is not None
            or kb._has_sticky_block(conn, task_id) or not isinstance(proof, dict)):
        return False
    stamp = kb._to_epoch(event["created_at"])
    if stamp is None or not 0 <= int(time.time()) - stamp < dispatch._RESPAWN_GUARD_PR_WINDOW:
        return False
    try:
        wait = proof["wait"]
        binding = _binding(wait["binding"])
        result = _result(proof["result"], binding)
        parked = _event(conn, task_id, "scheduled")
        if (not parked or parked["id"] != proof["scheduled_event_id"] or parked["id"] >= event["id"]
                or kb._json_dict(parked["payload"]).get("ci_wait") != wait
                or wait["task"] != task_id or wait["owner"] != task.assignee
                or parked["run_id"] != wait["run"] or not _run_matches(conn, task, wait["run"])
                or not _unchanged_since_park(conn, task_id, parked["id"], wait["run"]) or result["conclusion"] != "success"):
            return False
        url = f"https://github.com/{binding['repo']}/pull/{binding['pr']}"
        return bool(pr_comments) and all(
            comment["id"] <= wait["comment_id"]
            and all(match.group() == url for match in dispatch._RESPAWN_GUARD_PR_URL_RE.finditer(comment["body"]))
            for comment in pr_comments)
    except (KeyError, TypeError, ValueError):
        # Persisted optional proof is not authority when unreadable or partial.
        return False


def ci_claim_guarded(conn, task_id):
    """Revalidate CI-origin dispatch under the claim's write lock, not just its preview."""
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
    # A later ordinary unblock can replace the latest receipt between preview
    # and claim. It invalidates the grant, not the obligation to recheck it.
    ci_origin = any("ci_resume" in kb._json_dict(row["payload"]) for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'unblocked' ORDER BY id DESC", (task_id,)))
    return ci_origin and dispatch.check_respawn_guard(conn, task_id) is not None
