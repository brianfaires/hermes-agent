"""Continuation uses the board's existing subscription cursor, not chat ticks."""
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def test_semantic_idempotent_ack_requires_native_closure(board):
    from hermes_cli import kanban_continuation as continuation
    task = pending(board)
    payload = continuation.collect_wakeup(board, job_id="job1", profile="default")
    kb.block_task(board, task, reason="native escalation", kind="needs_input")
    effect = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
    metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
    metadata.update({f"reason_ack_event:{payload['reason']}": payload["event"],
                     f"reason_effect_event:{payload['reason']}": effect})
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_metadata=metadata)
    before = kb.list_notify_subs(board)
    assert not continuation.acknowledge_wakeup(
        board, job_id="job1", profile="default", card=task, event=payload["event"],
        reason=payload["reason"], effect_event=effect)
    assert kb.list_notify_subs(board) == before


def test_semantic_forged_scalar_effect_cannot_hide_obligation(board):
    from hermes_cli import kanban_continuation as continuation
    task = pending(board)
    payload = continuation.collect_wakeup(board, job_id="job1", profile="default")
    kb.block_task(board, task, reason="native but unacknowledged escalation", kind="needs_input")
    effect = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
    metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
    metadata.update({f"reason_ack_event:{payload['reason']}": payload["event"],
                     f"reason_effect_event:{payload['reason']}": effect})
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_metadata=metadata)
    with kb.connect_closing() as reopened:
        items = continuation.observe_pending_obligations(
            reopened, job_id="job1", profile="default", deadline_seconds=120)
        assert len(items) == 1, "scalar pointers are not a verified acknowledgement"
        assert not items[0]["admission_actionable"]


def test_semantic_verified_closure_is_quiet_until_new_episode(board):
    from hermes_cli import kanban_continuation as continuation
    task = pending(board)
    first = continuation.collect_wakeup(board, job_id="job1", profile="default")
    assert verified_ack(board, first)
    with kb.connect_closing() as reopened:
        assert continuation.observe_pending_obligations(
            reopened, job_id="job1", profile="default", deadline_seconds=120) == []
    kb.unblock_task(board, task)
    kb._record_task_failure(board, task, "authentication failed", outcome="spawn_failed", failure_limit=5)
    second = continuation.collect_wakeup(board, job_id="job1", profile="default")
    items = continuation.observe_pending_obligations(
        board, job_id="job1", profile="default", deadline_seconds=120)
    assert len(items) == 1 and second["event"] > first["event"]
    assert items[0]["episode_event"] == second["event"]


@pytest.mark.parametrize("invalid", ["wrong_reason", "stale_effect", "forged_receipt", "superseded"])
def test_semantic_observer_rejects_invalid_closure(board, invalid):
    from hermes_cli import kanban_continuation as continuation
    task = pending(board)
    payload = continuation.collect_wakeup(board, job_id="job1", profile="default")
    assert verified_ack(board, payload)
    metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
    reason = payload["reason"]
    if invalid == "superseded":
        kb.unblock_task(board, task)
        kb.block_task(board, task, reason="new protected hold", kind="capability")
    elif invalid == "wrong_reason":
        for prefix in ("observed", "ack", "effect", "closed"):
            metadata[f"reason_{prefix}_event:retry_exhausted"] = metadata[f"reason_{prefix}_event:{reason}"]
        del metadata[f"reason_observed_event:{reason}"]
    elif invalid == "stale_effect":
        metadata[f"reason_effect_event:{reason}"] = payload["event"]
    else:
        metadata[f"reason_closed_event:{reason}"] = metadata[f"reason_effect_event:{reason}"]
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_metadata=metadata)
    with kb.connect_closing() as reopened:
        assert len(continuation.observe_pending_obligations(
            reopened, job_id="job1", profile="default", deadline_seconds=120)) == 1


@pytest.mark.parametrize("state", ["unavailable", "dead", "stale"])
@pytest.mark.parametrize("crashed_attempt", [False, True])
def test_semantic_escalation_is_durable_without_replay(board, monkeypatch, state, crashed_attempt):
    from hermes_cli import kanban_continuation as continuation
    from cron import jobs
    task = pending(board)
    admitted = continuation.collect_wakeup(board, job_id="job1", profile="default")
    started = kb.list_notify_subs(board)[0]["delivery_metadata"]["reason_admitted_at:authentication_blocker"]
    job = dict(subscription_only=True, provider="openai-codex", model="gpt-6.1-sol",
               attach_to_session=False, prompt="bounded", script="private.py", monitor_script="private.py")
    monkeypatch.setattr(jobs, "get_job", lambda job_id: job if job_id == "job1" else None)
    monkeypatch.setattr(jobs, "is_job_runnable", lambda value: value is job)
    if crashed_attempt:
        with kb.write_txn(board):
            kb._append_event(board, task, "continuation_recovery_attempt", {"reason": admitted["reason"]})
    assert not hasattr(continuation, "recover_pending_obligation"), "MVP must not expose automatic replay"
    args = dict(job_id="job1", profile="default", card=task, reason=admitted["reason"],
                deadline_seconds=120, now=started + 120, coordinator_state=state,
                owner_gate=lambda item: True)
    signal = continuation.signal_pending_escalation(board, **args)
    assert signal["action"] == "owner_decision_required"
    assert signal["event"] == admitted["event"]
    metadata = kb.list_notify_subs(board)[0]["delivery_metadata"]
    metadata[f"reason_episode_event:{admitted['reason']}"] = admitted["event"] + 100
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_metadata=metadata)
    with kb.connect_closing() as reopened:
        assert continuation.signal_pending_escalation(reopened, **args) is None
        items = continuation.observe_pending_obligations(
            reopened, job_id="job1", profile="default", deadline_seconds=120, now=started + 121)
        assert len(items) == 1 and items[0]["last_verified_progress_at"] is None
        metadata = kb.list_notify_subs(reopened)[0]["delivery_metadata"]
        assert "reason_ack_event:authentication_blocker" not in metadata
        assert kb.get_task(reopened, task).status == "ready"
        assert reopened.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? "
                                "AND kind = 'continuation_escalation_required'", (task,)).fetchone()[0] == 1


@pytest.mark.parametrize("subscription_only", [False, True])
@pytest.mark.parametrize("gate_change", [None, "expiry", "job_pause"])
def test_semantic_escalation_with_advancing_clock_rechecks_live_gates(board, monkeypatch, gate_change, subscription_only):
    from cron.jobs import create_job, pause_job, use_cron_store
    from hermes_cli import kanban_continuation as continuation
    from hermes_constants import get_hermes_home

    with use_cron_store(get_hermes_home()):
        job = create_job(prompt="Read admitted card; decision only.", schedule="every 5m",
                         script="gate.py", monitor_script="gate.py", deliver="local",
                         provider="openai-codex", model="gpt-6.1-sol", subscription_only=subscription_only,
                         attach_to_session=False)
        task = new_task(board, subscription_only=subscription_only)
        subscribe(board, task)
        metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
        kb.remove_notify_sub(board, task_id=task, platform="continuation", chat_id="job1")
        assert kb.block_task(board, task, reason="real stalled capability", kind="capability")
        held = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
        metadata.update(decision_required=True, continuation_context="fresh", capability_decision_event=held)
        kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id=job["id"],
                          notifier_profile="default", delivery_mode="wake", delivery_metadata=metadata)
        admitted = continuation.collect_wakeup(board, job_id=job["id"], profile="default")
        assert admitted["wakeAgent"] is True
        item = continuation.observe_pending_obligations(
            board, job_id=job["id"], profile="default", deadline_seconds=120)[0]
        before = kb.list_notify_subs(board)
        clock = [item["deadline_at"] + 1]

        def advancing_time():
            clock[0] += 0.01
            return clock[0]

        monkeypatch.setattr(continuation.time, "time", advancing_time)

        def owner_gate(observation):
            if gate_change == "expiry":
                clock[0] = metadata["authority_expires_at"]
            elif gate_change == "job_pause":
                pause_job(job["id"])
            return True

        args = dict(job_id=job["id"], profile="default", card=task, reason=admitted["reason"],
                    deadline_seconds=120, coordinator_state="unavailable", owner_gate=owner_gate)
        signal = continuation.signal_pending_escalation(board, **args)
        if gate_change:
            assert signal is None
        else:
            assert signal is not None
            assert signal["episode_event"] == item["episode_event"]
            assert signal["deadline_at"] == item["deadline_at"]
            assert signal["overdue"] and signal["action"] == "owner_decision_required"
        with kb.connect_closing() as reopened:
            assert continuation.signal_pending_escalation(reopened, **args) is None
            assert kb.list_notify_subs(reopened) == before
            assert reopened.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? "
                                    "AND kind = 'continuation_escalation_required'", (task,)).fetchone()[0] == (0 if gate_change else 1)
            assert kb.get_task(reopened, task).status == "blocked"
            assert kb.has_active_control_hold(reopened, task)


@pytest.mark.parametrize("subscription_only", [False, True])
@pytest.mark.parametrize("gate", ["healthy", "before_deadline", "owner", "hold", "job", "inference"])
def test_semantic_escalation_gates_are_quiet(board, monkeypatch, gate, subscription_only):
    from cron import jobs
    from hermes_cli import kanban_continuation as continuation
    task = pending(board, subscription_only=subscription_only)
    payload = continuation.collect_wakeup(board, job_id="job1", profile="default")
    assert payload["wakeAgent"] is True
    started = kb.list_notify_subs(board)[0]["delivery_metadata"]["reason_admitted_at:authentication_blocker"]
    job = dict(subscription_only=subscription_only, provider="other" if gate == "inference" else "openai-codex", model="gpt-6.1-sol",
               attach_to_session=False, prompt="bounded", script="private.py", monitor_script="private.py")
    monkeypatch.setattr(jobs, "get_job", lambda job_id: None if gate == "job" else job)
    monkeypatch.setattr(jobs, "is_job_runnable", lambda value: True)
    if gate == "hold":
        kb.block_task(board, task, reason="operator stop", kind="needs_input")
    before = kb.list_notify_subs(board)

    assert continuation.signal_pending_escalation(
        board, job_id="job1", profile="default", card=task, reason=payload["reason"],
        deadline_seconds=120, now=started + (119 if gate == "before_deadline" else 120),
        coordinator_state="healthy" if gate == "healthy" else "dead",
        owner_gate=lambda item: gate != "owner") is None
    assert kb.list_notify_subs(board) == before


def test_semantic_pending_read_reopens_without_liveness_progress(board):
    from hermes_cli import kanban_continuation as continuation
    task = new_task(board)
    subscribe(board, task)
    kb._record_task_failure(board, task, "crashed", outcome="crashed", failure_limit=1)
    admitted = continuation.collect_wakeup(board, job_id="job1", profile="default")
    before = kb.list_notify_subs(board)
    assert hasattr(continuation, "observe_pending_obligations"), "missing independent semantic read"
    started = before[0]["delivery_metadata"]["reason_admitted_at:retry_exhausted"]
    kb.add_comment(board, task, author="worker", body="delivery/model succeeded")
    with kb.write_txn(board):
        kb._append_event(board, task, "heartbeat", {"note": "alive"})
    with kb.connect_closing() as reopened:
        items = continuation.observe_pending_obligations(
            reopened, job_id="job1", profile="default", deadline_seconds=120, now=started + 120)
        assert len(items) == 1
        assert items[0] == {
            "card": task, "reason": "retry_exhausted", "event": admitted["event"],
            "episode_event": admitted["event"],
            "profile": "default", "profile_home": str(Path.home() / ".hermes"),
            "job_id": "job1", "admitted_at": started, "admission_age_seconds": 120,
            "last_verified_progress_at": None, "deadline_at": started + 120,
            "overdue": True, "admission_actionable": True,
        }
        assert continuation.observe_pending_obligations(
            reopened, job_id="wrong", profile="default", deadline_seconds=120) == []
        assert continuation.observe_pending_obligations(
            reopened, job_id="job1", profile="ang", deadline_seconds=120) == []
        assert kb.list_notify_subs(reopened) == before


def test_semantic_read_retains_pending_hold_without_authorizing_action(board):
    from hermes_cli import kanban_continuation as continuation
    task = pending(board)
    continuation.collect_wakeup(board, job_id="job1", profile="default")
    assert kb.block_task(board, task, reason="operator hold", kind="needs_input")
    assert hasattr(continuation, "observe_pending_obligations"), "missing independent semantic read"
    before = kb.list_notify_subs(board)
    items = continuation.observe_pending_obligations(
        board, job_id="job1", profile="default", deadline_seconds=120)
    assert len(items) == 1 and not items[0]["admission_actionable"]
    assert kb.list_notify_subs(board) == before
    with pytest.raises(ValueError):
        continuation.observe_pending_obligations(
            board, job_id="job1", profile="default", deadline_seconds=0)


def test_explicit_decision_owner_admission_preserves_hold_until_control_effect(board):
    from hermes_cli import kanban_continuation as continuation
    task = new_task(board)
    subscribe(board, task)
    metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
    metadata.update(decision_required=True, continuation_context="fresh")
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_mode="wake", delivery_metadata=metadata)
    assert kb.block_task(board, task, reason="real owner input", kind="needs_input")
    payload = continuation.collect_wakeup(board, job_id="job1", profile="default")
    assert payload["reason"] == "decision_required"
    assert kb.get_task(board, task).status == "blocked"
    assert kb.has_active_control_hold(board, task)
    kb.add_comment(board, task, author="default", body="an answer is not a control effect")
    comment = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
    assert not continuation.acknowledge_wakeup(
        board, job_id="job1", profile="default", card=task, event=payload["event"],
        reason=payload["reason"], effect_event=comment)
    with kb.connect_closing() as reopened:
        items = continuation.observe_pending_obligations(
            reopened, job_id="job1", profile="default", deadline_seconds=900)
        assert len(items) == 1 and items[0]["reason"] == "decision_required"
        assert items[0]["admission_actionable"]
    assert kb.unblock_task(board, task)
    effect = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
    assert continuation.acknowledge_wakeup(
        board, job_id="job1", profile="default", card=task, event=payload["event"],
        reason=payload["reason"], effect_event=effect)
    assert continuation.observe_pending_obligations(
        board, job_id="job1", profile="default", deadline_seconds=900) == []


def test_exact_capability_decision_grant_surfaces_hold_without_releasing_it(board):
    from hermes_cli import kanban_continuation as continuation
    task = new_task(board)
    assert kb.block_task(board, task, reason="real owner capability boundary", kind="capability")
    held = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
    subscribe(board, task)
    metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
    metadata.update(decision_required=True, continuation_context="fresh")
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_mode="wake", delivery_metadata=metadata)
    assert continuation.collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}
    metadata["capability_decision_event"] = held
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_mode="wake", delivery_metadata=metadata)
    payload = continuation.collect_wakeup(board, job_id="job1", profile="default")
    assert payload["reason"] == "decision_required" and payload["event"] == held
    assert kb.get_task(board, task).status == "blocked"
    assert kb.has_active_control_hold(board, task)
    before = kb.list_notify_subs(board)
    with kb.connect_closing() as reopened:
        assert continuation.collect_wakeup(reopened, job_id="job1", profile="default") == payload
        assert kb.list_notify_subs(reopened) == before
    assert kb.unblock_task(board, task)
    assert kb.block_task(board, task, reason="later incident hold", kind="capability")
    assert continuation.collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}


def test_new_block_cannot_close_unresolved_decision(board):
    from hermes_cli import kanban_continuation as continuation
    task = new_task(board)
    subscribe(board, task)
    metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
    metadata.update(decision_required=True, continuation_context="fresh")
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_mode="wake", delivery_metadata=metadata)
    assert kb.block_task(board, task, reason="real owner decision", kind="needs_input")
    payload = continuation.collect_wakeup(board, job_id="job1", profile="default")
    assert kb.unblock_task(board, task)
    assert kb.block_task(board, task, reason="still unresolved capability", kind="capability")
    effect = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
    before = kb.list_notify_subs(board)
    assert not continuation.acknowledge_wakeup(
        board, job_id="job1", profile="default", card=task, event=payload["event"],
        reason=payload["reason"], effect_event=effect)
    assert kb.list_notify_subs(board) == before
    items = continuation.observe_pending_obligations(
        board, job_id="job1", profile="default", deadline_seconds=900)
    assert len(items) == 1 and items[0]["last_verified_progress_at"] is None


@pytest.mark.parametrize("observe_again", [False, True])
def test_legacy_blocked_decision_receipt_cannot_hide_pending_episode(board, observe_again):
    from hermes_cli import kanban_continuation as continuation
    from hermes_constants import get_hermes_home
    task = new_task(board)
    subscribe(board, task)
    metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
    metadata.update(decision_required=True, continuation_context="fresh")
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_mode="wake", delivery_metadata=metadata)
    assert kb.block_task(board, task, reason="owner decision", kind="needs_input")
    payload = continuation.collect_wakeup(board, job_id="job1", profile="default")
    assert kb.unblock_task(board, task)
    assert kb.block_task(board, task, reason="still stalled", kind="capability")
    effect = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
    # Persist an actual old-version native closure, then read with new semantics.
    with kb.write_txn(board):
        kb._append_event(board, task, "continuation_reason_closed", {
            "reason": payload["reason"], "event": payload["event"], "effect_event": effect,
            "profile": "default", "job_id": "job1", "profile_home": str(get_hermes_home()),
        })
    receipt = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
    metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
    metadata.update({"reason_ack_event:decision_required": payload["event"],
                     "reason_effect_event:decision_required": effect,
                     "reason_closed_event:decision_required": receipt,
                     "capability_decision_event": effect})
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_mode="wake", delivery_metadata=metadata)
    with kb.connect_closing() as reopened:
        items = continuation.observe_pending_obligations(
            reopened, job_id="job1", profile="default", deadline_seconds=900)
        assert len(items) == 1, "another block did not resolve the owner decision"
        episode, admitted_at = items[0]["episode_event"], items[0]["admitted_at"]
        current = payload
        if observe_again:
            current = continuation.collect_wakeup(reopened, job_id="job1", profile="default")
            assert current["event"] == effect
        retained = continuation.observe_pending_obligations(
            reopened, job_id="job1", profile="default", deadline_seconds=900)
        assert retained[0]["episode_event"] == episode == payload["event"]
        assert retained[0]["admitted_at"] == admitted_at
        assert kb.unblock_task(reopened, task)
        resolved = reopened.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
        assert continuation.acknowledge_wakeup(
            reopened, job_id="job1", profile="default", card=task, event=current["event"],
            reason=current["reason"], effect_event=resolved)
        assert continuation.observe_pending_obligations(
            reopened, job_id="job1", profile="default", deadline_seconds=900) == []


def test_blocked_poll_has_no_wake_or_cursor_change(board):
    task = new_task(board)
    kb.block_task(board, task, reason="needs input", kind="needs_input")
    subscribe(board, task)
    before = kb.list_notify_subs(board)
    from hermes_cli.kanban_continuation import collect_wakeup
    for _ in range(3):
        assert collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}
    assert kb.list_notify_subs(board) == before


def test_ack_requires_verified_native_effect(board):
    from hermes_cli.kanban_continuation import acknowledge_wakeup, collect_wakeup
    task = pending(board)
    payload = collect_wakeup(board, job_id="job1", profile="default")
    assert not acknowledge_wakeup(board, job_id="job1", profile="default",
                                  card=task, event=payload["event"])
    assert collect_wakeup(board, job_id="job1", profile="default") == payload


def test_related_exception_batch_retains_each_reason(board):
    from hermes_cli.kanban_continuation import collect_wakeup
    first = pending(board)
    second = new_task(board)
    subscribe(board, second)
    kb._record_task_failure(board, second, "crashed", outcome="crashed", failure_limit=1)
    payload = collect_wakeup(board, job_id="job1", profile="default")
    assert {(item["card"], item["reason"]) for item in payload["exceptions"]} == {
        (first, "authentication_blocker"), (second, "retry_exhausted")}


def test_ack_binds_reason_to_observed_event(board):
    from hermes_cli.kanban_continuation import acknowledge_wakeup, collect_wakeup
    task = pending(board)
    payload = collect_wakeup(board, job_id="job1", profile="default")
    assert kb.block_task(board, task, reason="verified escalation", kind="needs_input")
    effect = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
    before = kb.list_notify_subs(board)
    assert not acknowledge_wakeup(board, job_id="job1", profile="default", card=task,
                                  event=payload["event"], reason="retry_exhausted", effect_event=effect)
    assert kb.list_notify_subs(board) == before
    assert acknowledge_wakeup(board, job_id="job1", profile="default", card=task,
                              event=payload["event"], reason=payload["reason"], effect_event=effect)


def test_restart_replay_changed_reason_and_partial_batch(board):
    from hermes_cli.kanban_continuation import acknowledge_wakeup, collect_wakeup
    first, second = pending(board), pending(board)
    batch = collect_wakeup(board, job_id="job1", profile="default")
    item = next(item for item in batch["exceptions"] if item["card"] == first)
    assert verified_ack(board, item)
    effect = kb.list_notify_subs(board, first)[0]["delivery_metadata"][f"reason_effect_event:{item['reason']}"]
    with kb.connect_closing() as restarted:
        assert acknowledge_wakeup(restarted, job_id="job1", profile="default", card=first,
                                  event=item["event"], reason=item["reason"], effect_event=effect)
        assert collect_wakeup(restarted, job_id="job1", profile="default")["card"] == second
        assert kb.unblock_task(restarted, first)
        assert kb._record_task_failure(restarted, first, "worker crashed", outcome="crashed", failure_limit=1)
        changed = collect_wakeup(restarted, job_id="job1", profile="default")
        assert (first, "retry_exhausted") in {(x["card"], x["reason"]) for x in changed["exceptions"]}
        retained = kb.list_notify_subs(restarted, first)[0]["delivery_metadata"]
        assert retained[f"reason_ack_event:{item['reason']}"] == item["event"]
        assert retained[f"reason_effect_event:{item['reason']}"] == effect


def test_exact_named_profile_ack_with_valid_evidence(board, monkeypatch):
    from hermes_cli.kanban_continuation import acknowledge_wakeup, collect_wakeup
    task = new_task(board)
    assert kb.block_task(board, task, reason="temporary wait", kind="transient")
    subscribe(board, task)
    metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job-ang",
                      notifier_profile="ang", delivery_metadata=metadata)
    # Native subscriptions intentionally snapshot history at registration.
    # Both observers must exist before the exception they are testing.
    assert kb.unblock_task(board, task)
    kb._record_task_failure(board, task, "authentication failed", outcome="spawn_failed", failure_limit=5)
    default = collect_wakeup(board, job_id="job1", profile="default")
    assert collect_wakeup(board, job_id="job-ang", profile="ang") == {"wakeAgent": False}
    home = Path.home() / ".hermes" / "profiles" / "ang"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "ang")
    named = collect_wakeup(board, job_id="job-ang", profile="ang")
    assert named["card"] == task
    assert kb.block_task(board, task, reason="verified escalation", kind="needs_input")
    effect = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
    before = kb.list_notify_subs(board)
    assert not acknowledge_wakeup(board, job_id="job1", profile="default", card=task,
                                  event=default["event"], reason=default["reason"], effect_event=effect)
    assert not acknowledge_wakeup(board, job_id="job1", profile="ang", card=task,
                                  event=named["event"], reason=named["reason"], effect_event=effect)
    with monkeypatch.context() as wrong_home:
        wrong_home.setenv("HERMES_HOME", str(Path.home() / ".hermes"))
        assert collect_wakeup(board, job_id="job-ang", profile="ang") == {"wakeAgent": False}
        assert not acknowledge_wakeup(board, job_id="job-ang", profile="ang", card=task,
                                      event=named["event"], reason=named["reason"], effect_event=effect)
    assert kb.list_notify_subs(board) == before
    assert acknowledge_wakeup(board, job_id="job-ang", profile="ang", card=task,
                              event=named["event"], reason=named["reason"], effect_event=effect)
    default_sub = next(sub for sub in kb.list_notify_subs(board) if sub["chat_id"] == "job1")
    assert not any(key.startswith("reason_ack_event:") for key in default_sub["delivery_metadata"])


@pytest.mark.parametrize("invalid", ["comment", "heartbeat", "other_task"])
def test_invalid_effect_evidence_keeps_admission(board, invalid):
    from hermes_cli.kanban_continuation import acknowledge_wakeup, collect_wakeup
    task = pending(board)
    payload = collect_wakeup(board, job_id="job1", profile="default")
    if invalid == "comment":
        kb.add_comment(board, task, author="worker", body="model succeeded")
    elif invalid == "heartbeat":
        with kb.write_txn(board):
            kb._append_event(board, task, "heartbeat", {"note": "alive"})
    else:
        other = new_task(board)
        kb.block_task(board, other, reason="other escalation", kind="needs_input")
    effect = board.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
    before = kb.list_notify_subs(board)
    assert not acknowledge_wakeup(board, job_id="job1", profile="default", card=task,
                                  event=payload["event"], reason=payload["reason"], effect_event=effect)
    assert kb.list_notify_subs(board) == before
    assert collect_wakeup(board, job_id="job1", profile="default")["card"] == task


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "profiles" / "ang").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "board.db"))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    with kb.connect_closing() as conn:
        yield conn


def subscribe(conn, task):
    kb.add_notify_sub(conn, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_metadata={
                          "authority_actor": "Brian", "authority_reference": "test scoped approval",
                          "authority_expires_at": int(time.time()) + 600,
                          "authority_task_id": task, "authority_assignee": "ang",
                          "procedure": "autonomous-work-continuation"})


def new_task(conn, subscription_only=True, **kwargs):
    return kb.create_task(conn, title="Authorized work", assignee="ang",
                          subscription_only=subscription_only, model_override="gpt-6.1-sol",
                          provider_override="openai-codex", **kwargs)


def verified_ack(conn, payload):
    from hermes_cli.kanban_continuation import acknowledge_wakeup
    assert kb.block_task(conn, payload["card"], reason="verified escalation", kind="needs_input")
    effect = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?",
                          (payload["card"],)).fetchone()[0]
    return acknowledge_wakeup(conn, job_id="job1", profile="default",
                              card=payload["card"], event=payload["event"],
                              reason=payload["reason"], effect_event=effect)


@pytest.mark.parametrize("transition", ["ready", "review", "done"])
def test_healthy_native_transitions_stay_silent(board, transition):
    from hermes_cli.kanban_continuation import collect_wakeup
    task = new_task(board)
    subscribe(board, task)
    if transition == "ready":
        kb.block_task(board, task, reason="temporary wait", kind="transient")
        kb.unblock_task(board, task)
    elif transition == "review":
        assert kb.request_review(board, task, summary="implementation verified")
    else:
        kb.complete_task(board, task, summary="ordinary leaf result")
    before = kb.list_notify_subs(board)
    for _ in range(3):
        assert collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}
    assert kb.list_notify_subs(board) == before


def test_explicit_goal_closeout_is_admitted(board):
    from hermes_cli.kanban_continuation import collect_wakeup
    task = new_task(board)
    child = new_task(board, parents=[task])
    subscribe(board, task)
    metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
    metadata["goal_closeout"] = True
    metadata["goal_task_id"] = task
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_metadata=metadata)
    kb.complete_task(board, task, summary="verified goal outcome")
    kb.complete_task(board, child, summary="verified child outcome")
    result = collect_wakeup(board, job_id="job1", profile="default")
    assert result["card"] == task and result["reason"] == "goal_closeout"


@pytest.mark.parametrize("wrong_identity", [None, "reason", "event", "profile", "job_id", "profile_home"])
def test_native_terminal_goal_receipt_ack_and_restart(board, wrong_identity):
    from hermes_cli.kanban_continuation import acknowledge_wakeup, collect_wakeup
    task = new_task(board)
    child = new_task(board, parents=[task])
    subscribe(board, task)
    metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
    metadata.update(goal_closeout=True, goal_task_id=task)
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_metadata=metadata)
    assert kb.complete_task(board, task, summary="verified goal outcome")
    assert kb.complete_task(board, child, summary="verified child outcome")
    payload = collect_wakeup(board, job_id="job1", profile="default")
    receipt = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'completed'",
                            (task,)).fetchone()[0]
    assert payload["reason"] == "goal_closeout" and receipt == payload["event"]
    if wrong_identity:
        from hermes_constants import get_hermes_home
        identity = {"reason": payload["reason"], "event": payload["event"],
                    "profile": "default", "job_id": "job1",
                    "profile_home": str(get_hermes_home())}
        identity[wrong_identity] = "wrong"
        with kb.write_txn(board):
            kb._append_event(board, task, "continuation_reason_admitted", identity)
        assert not acknowledge_wakeup(board, job_id="job1", profile="default", card=task,
                                      event=payload["event"], reason=payload["reason"], effect_event=receipt)
        return
    assert acknowledge_wakeup(board, job_id="job1", profile="default", card=task,
                              event=payload["event"], reason=payload["reason"], effect_event=receipt)
    with kb.connect_closing() as restarted:
        assert acknowledge_wakeup(restarted, job_id="job1", profile="default", card=task,
                                  event=payload["event"], reason=payload["reason"], effect_event=receipt)
        assert collect_wakeup(restarted, job_id="job1", profile="default") == {"wakeAgent": False}


@pytest.mark.parametrize("invalid", ["wrong_reason", "wrong_card", "other_receipt",
                                     "comment", "revoked_goal", "unfinished_child"])
def test_terminal_goal_rejects_invalid_receipt(board, invalid):
    from hermes_cli.kanban_continuation import acknowledge_wakeup, collect_wakeup
    task = new_task(board)
    child = new_task(board, parents=[task])
    subscribe(board, task)
    metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
    metadata.update(goal_closeout=True, goal_task_id=task)
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_metadata=metadata)
    assert kb.complete_task(board, task, summary="verified goal outcome")
    assert kb.complete_task(board, child, summary="verified child outcome")
    payload = collect_wakeup(board, job_id="job1", profile="default")
    card, reason, receipt = task, payload["reason"], payload["event"]
    if invalid == "wrong_reason":
        reason = "authentication_blocker"
    elif invalid == "wrong_card":
        card = child
    elif invalid == "other_receipt":
        receipt = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (child,)).fetchone()[0]
    elif invalid == "comment":
        kb.add_comment(board, task, author="worker", body="model completed")
        receipt = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
    elif invalid == "revoked_goal":
        metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
        metadata["goal_closeout"] = False
        kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                          notifier_profile="default", delivery_metadata=metadata)
    else:
        new_task(board, parents=[task])
    before = kb.list_notify_subs(board)
    assert not acknowledge_wakeup(board, job_id="job1", profile="default", card=card,
                                  event=payload["event"], reason=reason, effect_event=receipt)
    assert kb.list_notify_subs(board) == before


def test_effect_status_mismatch_rejected(board):
    from hermes_cli.kanban_continuation import acknowledge_wakeup, collect_wakeup
    task = pending(board)
    payload = collect_wakeup(board, job_id="job1", profile="default")
    assert kb.block_task(board, task, reason="verified escalation", kind="needs_input")
    receipt = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
    assert kb.unblock_task(board, task)
    before = kb.list_notify_subs(board)
    assert not acknowledge_wakeup(board, job_id="job1", profile="default", card=task,
                                  event=payload["event"], reason=payload["reason"], effect_event=receipt)
    assert kb.list_notify_subs(board) == before


def test_superseded_same_status_effect_rejected(board):
    from hermes_cli.kanban_continuation import acknowledge_wakeup, collect_wakeup
    task = pending(board)
    payload = collect_wakeup(board, job_id="job1", profile="default")
    assert kb.block_task(board, task, reason="first escalation", kind="needs_input")
    old_effect = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
    assert kb.unblock_task(board, task)
    assert kb.block_task(board, task, reason="new distinct escalation", kind="capability")
    fresh_effect = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (task,)).fetchone()[0]
    before = kb.list_notify_subs(board)
    assert not acknowledge_wakeup(board, job_id="job1", profile="default", card=task,
                                  event=payload["event"], reason=payload["reason"], effect_event=old_effect)
    assert kb.list_notify_subs(board) == before
    assert acknowledge_wakeup(board, job_id="job1", profile="default", card=task,
                              event=payload["event"], reason=payload["reason"], effect_event=fresh_effect)


def pending(conn, **kwargs):
    task = new_task(conn, **kwargs)
    kb.block_task(conn, task, reason="temporary wait", kind="transient")
    subscribe(conn, task)
    kb.unblock_task(conn, task)
    kb._record_task_failure(conn, task, "authentication failed", outcome="spawn_failed", failure_limit=5)
    return task


def test_eligible_transition_is_compact_and_claimed_once(board):
    from hermes_cli.kanban_continuation import acknowledge_wakeup, collect_wakeup
    task = pending(board)
    payload = collect_wakeup(board, job_id="job1", profile="default")
    assert payload["wakeAgent"] is True
    assert payload["card"] == task and payload["reason"] == "authentication_blocker"
    assert len(json.dumps(payload)) < 300
    assert verified_ack(board, payload)
    assert collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}


def test_observation_does_not_acknowledge_before_coordinator_success(board):
    from hermes_cli.kanban_continuation import collect_wakeup
    task = pending(board)
    before = kb.list_notify_subs(board)
    first = collect_wakeup(board, job_id="job1", profile="default")
    assert first["wakeAgent"]
    assert kb.list_notify_subs(board)[0]["last_event_id"] == before[0]["last_event_id"]
    observed = kb.list_notify_subs(board)
    assert collect_wakeup(board, job_id="job1", profile="default") == first
    assert kb.list_notify_subs(board) == observed


@pytest.mark.parametrize("change", [
    {"authority_actor": "worker"}, {"authority_reference": ""},
    {"authority_expires_at": 0}, {"authority_expires_at": True},
    {"authority_task_id": "t_12345678"}, {"authority_assignee": "ops"},
    {"procedure": "wrong"},
])
def test_invalid_or_expired_grant_is_not_authority(board, change):
    from hermes_cli.kanban_continuation import collect_wakeup
    task = pending(board)
    metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
    metadata.update(change)
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_metadata=metadata)
    before = kb.list_notify_subs(board)
    assert collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}
    assert kb.list_notify_subs(board) == before


def test_routing_capacity_and_live_worker_are_gates(board):
    import os
    from hermes_cli.kanban_continuation import collect_wakeup
    task = pending(board)
    assert collect_wakeup(board, job_id="wrong", profile="default") == {"wakeAgent": False}
    assert collect_wakeup(board, job_id="job1", profile="ang") == {"wakeAgent": False}
    active = new_task(board)
    kb.claim_task(board, active)
    assert collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}
    kb.complete_task(board, active)
    assert collect_wakeup(board, job_id="job1", profile="default")["card"] == task
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET last_failure_error = NULL WHERE id = ?", (task,))
    spawned = kb.dispatch_once(board, spawn_fn=lambda *args, **kwargs: os.getpid(),
                               max_in_progress=1, max_in_progress_per_profile=1)
    assert len(spawned.spawned) == 1
    assert kb.get_task(board, task).status == "running"
    assert collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}
    assert kb.dispatch_once(board, spawn_fn=lambda *args, **kwargs: os.getpid(),
                            max_in_progress=1).spawned == []


def test_dependency_completion_promotes_and_closeout_is_acknowledged(board):
    from hermes_cli.kanban_continuation import acknowledge_wakeup, collect_wakeup
    parent = new_task(board)
    child = new_task(board, parents=[parent])
    subscribe(board, parent)
    subscribe(board, child)
    assert collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}
    kb.complete_task(board, parent, summary="bounded result")
    first = collect_wakeup(board, job_id="job1", profile="default")
    assert first == {"wakeAgent": False}
    assert kb.get_task(board, child).status == "ready"
    kb._record_task_failure(board, child, "authentication failed", outcome="spawn_failed", failure_limit=5)
    second = collect_wakeup(board, job_id="job1", profile="default")
    assert second["card"] == child and second["reason"] == "authentication_blocker"
    assert verified_ack(board, second)
    assert collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}


def test_heartbeat_and_comment_do_not_rearm_acknowledged_event(board):
    from hermes_cli.kanban_continuation import acknowledge_wakeup, collect_wakeup
    task = pending(board)
    event = collect_wakeup(board, job_id="job1", profile="default")
    assert verified_ack(board, event)
    kb.add_comment(board, task, author="worker", body="heartbeat commentary")
    with kb.write_txn(board):
        kb._append_event(board, task, "heartbeat", {"note": "alive"})
    assert collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}


def test_dead_worker_is_recovered_only_by_native_dispatcher(board, monkeypatch):
    import socket
    from hermes_cli.kanban_continuation import collect_wakeup
    task = new_task(board)
    subscribe(board, task)
    kb.claim_task(board, task, claimer=f"{socket.gethostname()}:99999999")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (99999999, task))
    assert collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}
    assert kb.detect_crashed_workers(board) == []
    started = kb.get_task(board, task).started_at
    monkeypatch.setattr(kb.time, "time", lambda: started + kb._resolve_crash_grace_seconds() + 1)
    assert task in kb.detect_crashed_workers(board)
    assert kb.get_task(board, task).status == "ready"
    assert collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}


def test_ack_cannot_consume_another_task_or_regress_cursor(board):
    from hermes_cli.kanban_continuation import acknowledge_wakeup, collect_wakeup
    task = pending(board)
    other = pending(board)
    event = collect_wakeup(board, job_id="job1", profile="default")
    assert not acknowledge_wakeup(board, job_id="job1", profile="default", card=other,
                                  event=event["event"])
    assert not acknowledge_wakeup(board, job_id="job1", profile="ang", card=task,
                                  event=event["event"])


def test_retry_exhaustion_and_later_operator_hold(board):
    from hermes_cli.kanban_continuation import collect_wakeup
    task = new_task(board)
    subscribe(board, task)
    assert kb._record_task_failure(board, task, "worker crashed", outcome="crashed", failure_limit=1)
    assert collect_wakeup(board, job_id="job1", profile="default")["reason"] == "retry_exhausted"
    # block_task deliberately does not replace an already-blocked card's hold.
    # Install the new operator hold through supported transitions.
    assert kb.unblock_task(board, task)
    assert kb.block_task(board, task, reason="operator hold", kind="needs_input")
    assert collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}


@pytest.mark.parametrize("goal_id", [None, "t_12345678", "self"])
def test_invalid_or_ordinary_leaf_goal_marker_is_silent(board, goal_id):
    from hermes_cli.kanban_continuation import collect_wakeup
    task = new_task(board)
    subscribe(board, task)
    metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
    metadata.update(goal_closeout=True, goal_task_id=task if goal_id == "self" else goal_id)
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_metadata=metadata)
    kb.complete_task(board, task)
    assert collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}


@pytest.mark.parametrize("goal_id", [None, "t_12345678", "self"])
def test_goal_identity_and_unfinished_children_do_not_close_goal(board, goal_id):
    from hermes_cli.kanban_continuation import collect_wakeup
    task = new_task(board)
    child = new_task(board, parents=[task])
    subscribe(board, task)
    metadata = kb.list_notify_subs(board, task)[0]["delivery_metadata"]
    metadata.update(goal_closeout=True, goal_task_id=task if goal_id == "self" else goal_id)
    kb.add_notify_sub(board, task_id=task, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_metadata=metadata)
    kb.complete_task(board, task)
    assert collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}
    kb.complete_task(board, child)
    result = collect_wakeup(board, job_id="job1", profile="default")
    if goal_id == "self":
        assert result["reason"] == "goal_closeout"
    else:
        assert result == {"wakeAgent": False}


def test_separate_smoke_stimulus_decision_effect_ack_and_two_quiet_repeats(board):
    """Native decision/receipt boundary only: no installed provider success claim."""
    from hermes_cli import kanban_continuation as continuation
    controller = new_task(board)
    stimulus = new_task(board)
    assert stimulus != controller
    subscribe(board, stimulus)
    metadata = kb.list_notify_subs(board, stimulus)[0]["delivery_metadata"]
    metadata.update(decision_required=True, continuation_context="fresh")
    kb.add_notify_sub(board, task_id=stimulus, platform="continuation", chat_id="job1",
                      notifier_profile="default", delivery_mode="wake", delivery_metadata=metadata)
    assert kb.block_task(board, stimulus, reason="Separate scoped owner decision", kind="needs_input")
    decision = continuation.collect_wakeup(board, job_id="job1", profile="default")
    assert decision["card"] == stimulus and decision["reason"] == "decision_required"
    # An explicit native owner action is required; delivery alone is not it.
    kb.add_comment(board, stimulus, author="default", body="Transport receipt only")
    receipt = kb.list_events(board, stimulus)[-1].id
    ack = dict(job_id="job1", profile="default", card=stimulus,
               event=decision["event"], reason=decision["reason"])
    assert not continuation.acknowledge_wakeup(board, **ack, effect_event=receipt)
    assert kb.unblock_task(board, stimulus)
    effect = kb.list_events(board, stimulus)[-1].id
    assert continuation.acknowledge_wakeup(board, **ack, effect_event=effect)
    # Real fresh connection; the close/reopen wait test separately destroys
    # the original connection to verify full persistence of that lifecycle.
    with kb.connect_closing() as reopened:
        before = kb.list_events(reopened, stimulus)
        for _ in range(2):
            assert continuation.collect_wakeup(reopened, job_id="job1", profile="default") == {"wakeAgent": False}
        assert kb.list_events(reopened, stimulus) == before
        assert kb.get_task(reopened, controller).status == "ready"


def test_standing_selected_leaf_and_exact_launch(board):
    from cron.jobs import create_job, use_cron_store
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_continuation as c
    with use_cron_store(get_hermes_home()):
        job = create_job(prompt="Bounded selection", schedule="every 5m", deliver="local",
                         script="gate.py", monitor_script="gate.py",
                         model="user-pin", attach_to_session=False)
        card = new_task(board)
        c.register_standing_coordination(board, card=card, profile="default", job_id=job['id'],
                                         authority_actor="Brian", authority_reference="test attributable request")
        kb.complete_task(board, card)
        wake = c.collect_wakeup(board, job_id=job['id'], profile="default")
        assert wake['reason'] == 'next_selection'
        target = new_task(board)
        c.register_standing_coordination(board, card=target, profile="default", job_id=job['id'],
                                         authority_actor="Brian", authority_reference="separate target approval")
        args = dict(card=card, event=wake['event'], job_id=job['id'], profile='default', next_card=target)
        assert c.bind_next_selection(board, **args)
        assert not c.acknowledge_next_selection(board, **args, effect_event=wake['event'])
        kb.claim_task(board, target)
        kb._set_worker_pid(board, target, 12345)
        effect = board.execute('SELECT MAX(id) FROM task_events WHERE task_id=?', (target,)).fetchone()[0]
        assert c.acknowledge_next_selection(board, **args, effect_event=effect)
        with kb.connect_closing() as reopened:
            assert c.acknowledge_next_selection(reopened, **args, effect_event=effect)
            assert c.collect_wakeup(reopened, job_id=job['id'], profile='default') == {'wakeAgent': False}
        assert kb.get_task(board, target).model_override == 'gpt-6.1-sol'


@pytest.fixture
def standing(board):
    from cron.jobs import create_job, use_cron_store
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_continuation as c
    with use_cron_store(get_hermes_home()):
        job = create_job(prompt="Bounded selection", schedule="every 5m", deliver="local",
                         script="gate.py", monitor_script="gate.py",
                         model="user-chosen-coordinator", attach_to_session=False)
        cards = [kb.create_task(board, title="Existing bounded work", assignee="ang",
                                provider_override="chosen-provider", model_override="chosen-model")
                 for _ in range(2)]
        for card in cards:
            c.register_standing_coordination(board, card=card, profile="default", job_id=job['id'],
                                             authority_actor="Brian", authority_reference=f"request:{card}")
        yield c, job, *cards


@pytest.mark.parametrize('finish', ['done', 'blocked'])
def test_standing_retains_blocker_and_recovers_missed_event(board, standing, finish):
    c, job, card, target = standing
    assert c.collect_wakeup(board, job_id=job['id'], profile='default') == {'wakeAgent': False}
    if finish == 'done':
        kb.complete_task(board, card)
    else:
        kb.block_task(board, card, reason='retained owner decision', kind='needs_input')
    with kb.connect_closing() as reopened:
        first = c.collect_wakeup(reopened, job_id=job['id'], profile='default')
        assert first['reason'] == 'next_selection'
        assert c.collect_wakeup(reopened, job_id=job['id'], profile='default') == first
        args = dict(card=card, event=first['event'], profile='default', job_id=job['id'], next_card=target)
        assert c.bind_next_selection(reopened, **args)
        assert c.bind_next_selection(reopened, **args)
        kb.add_comment(reopened, target, author='worker', body='model success')
        comment = kb.list_events(reopened, target)[-1].id
        assert not c.acknowledge_next_selection(reopened, **args, effect_event=comment)
        assert kb.claim_task(reopened, target)
        claimed = kb.list_events(reopened, target)[-1].id
        assert not c.acknowledge_next_selection(reopened, **args, effect_event=claimed)
        kb._set_worker_pid(reopened, target, 12345)
        spawned = kb.list_events(reopened, target)[-1].id
        assert c.acknowledge_next_selection(reopened, **args, effect_event=spawned)
        assert not c.acknowledge_wakeup(reopened, card=card, event=first['event'],
                                       profile='default', job_id=job['id'],
                                       reason='decision_required', effect_event=spawned)
        assert c.collect_wakeup(reopened, job_id=job['id'], profile='default') == {'wakeAgent': False}
    assert kb.get_task(board, card).status == finish
    assert kb.has_active_control_hold(board, card) == (finish == 'blocked')
    assert kb.get_task(board, target).model_override == 'chosen-model'
    assert kb.get_task(board, target).provider_override == 'chosen-provider'


@pytest.mark.parametrize('gate', ['paused', 'revoked', 'expired', 'job_pause', 'wrong_job',
                                  'wrong_owner', 'wrong_home', 'estop', 'capacity'])
def test_standing_admission_gates(board, standing, monkeypatch, gate):
    from cron.jobs import pause_job
    c, job, card, target = standing
    kb.complete_task(board, card)
    sub = _standing_subscription(board, card)
    metadata = sub['delivery_metadata']
    if gate in ('paused', 'revoked'):
        metadata['authority_' + gate] = True
    elif gate == 'expired':
        metadata['authority_expires_at'] = int(time.time()) - 1
    elif gate == 'wrong_home':
        metadata['authority_home'] = '/wrong/home'
    elif gate == 'job_pause':
        pause_job(job['id'])
    elif gate == 'estop':
        from agent.estop import engage
        engage('test stop in disposable home')
    elif gate == 'capacity':
        kb.claim_task(board, target)
    kb.add_notify_sub(board, task_id=card, platform='continuation', chat_id=job['id'],
                      notifier_profile='default', delivery_metadata=metadata)
    before = kb.list_notify_subs(board)
    assert c.collect_wakeup(board, job_id='wrong' if gate == 'wrong_job' else job['id'],
                            profile='ang' if gate == 'wrong_owner' else 'default') == {'wakeAgent': False}
    assert kb.list_notify_subs(board) == before


def _standing_subscription(board, card):
    return next(s for s in kb.list_notify_subs(board, card) if s['platform'] == 'continuation')


@pytest.mark.parametrize('gate', ['unregistered', 'expired', 'revoked', 'hold', 'parents', 'claimed'])
def test_standing_target_requires_independent_authority_and_gates(board, standing, gate):
    c, job, card, target = standing
    kb.complete_task(board, card)
    wake = c.collect_wakeup(board, job_id=job['id'], profile='default')
    if gate == 'unregistered':
        kb.remove_notify_sub(board, task_id=target, platform='continuation', chat_id=job['id'])
    elif gate in ('expired', 'revoked'):
        m = _standing_subscription(board, target)['delivery_metadata']
        m['authority_expires_at' if gate == 'expired' else 'authority_revoked'] = 1 if gate == 'expired' else True
        kb.add_notify_sub(board, task_id=target, platform='continuation', chat_id=job['id'],
                          notifier_profile='default', delivery_metadata=m)
    elif gate == 'hold':
        kb.block_task(board, target, reason='operator stop', kind='needs_input')
    elif gate == 'parents':
        parent = new_task(board)
        kb.link_tasks(board, parent, target)
    else:
        kb.claim_task(board, target)
    assert not c.bind_next_selection(board, card=card, event=wake['event'], profile='default',
                                     job_id=job['id'], next_card=target)


def test_standing_registration_cannot_launder_legacy_grant(board):
    from hermes_cli import kanban_continuation as c
    card = new_task(board)
    subscribe(board, card)
    m = kb.list_notify_subs(board, card)[0]['delivery_metadata']
    m['authority_expires_at'] = 1
    kb.add_notify_sub(board, task_id=card, platform='continuation', chat_id='job1',
                      notifier_profile='default', delivery_metadata=m)
    before = kb.list_notify_subs(board)
    for job in ('job1', 'another-job'):
        with pytest.raises(ValueError):
            c.register_standing_coordination(board, card=card, profile='default', job_id=job,
                                             authority_actor='Brian', authority_reference='old request')
    assert kb.list_notify_subs(board) == before
    assert c.collect_wakeup(board, job_id='job1', profile='default') == {'wakeAgent': False}


@pytest.mark.parametrize('gate', ['source_revoke', 'target_revoke', 'job_pause', 'estop', 'wrong_owner', 'wrong_job'])
def test_standing_ack_rechecks_gates(board, standing, monkeypatch, gate):
    from cron.jobs import pause_job
    c, job, card, target = standing
    kb.complete_task(board, card)
    event = c.collect_wakeup(board, job_id=job['id'], profile='default')['event']
    args = dict(card=card, event=event, profile='default', job_id=job['id'], next_card=target)
    assert c.bind_next_selection(board, **args)
    kb.claim_task(board, target)
    kb._set_worker_pid(board, target, 12345)
    effect = kb.list_events(board, target)[-1].id
    if gate.endswith('revoke'):
        subject = card if gate == 'source_revoke' else target
        m = _standing_subscription(board, subject)['delivery_metadata']
        m['authority_revoked'] = True
        kb.add_notify_sub(board, task_id=subject, platform='continuation', chat_id=job['id'],
                          notifier_profile='default', delivery_metadata=m)
    elif gate == 'job_pause':
        pause_job(job['id'])
    elif gate == 'estop':
        from agent.estop import engage
        engage('test stop in disposable home')
    elif gate == 'wrong_owner':
        args['profile'] = 'ang'
    else:
        args['job_id'] = 'wrong'
    assert not c.acknowledge_next_selection(board, **args, effect_event=effect)
    assert not any(e.kind == 'continuation_selection_closed' for e in kb.list_events(board, card))


def test_standing_target_cannot_be_reserved_twice(board, standing):
    c, job, card, target = standing
    other = new_task(board)
    c.register_standing_coordination(board, card=other, profile='default', job_id=job['id'],
                                     authority_actor='Brian', authority_reference='other selected source')
    for source in (card, other):
        kb.complete_task(board, source)
    events = {p['card']: p['event'] for p in
              c.collect_wakeup(board, job_id=job['id'], profile='default')['exceptions']}
    assert c.bind_next_selection(board, card=card, event=events[card], profile='default',
                                 job_id=job['id'], next_card=target)
    with kb.connect_closing() as reopened:
        assert not c.bind_next_selection(reopened, card=other, event=events[other], profile='default',
                                         job_id=job['id'], next_card=target)


def test_standing_leaf_native_admission_contract(board):
    """Discriminates existing admission behavior without calling a new API."""
    from cron.jobs import create_job, use_cron_store
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_continuation as c
    with use_cron_store(get_hermes_home()):
        job = create_job(prompt='Bounded selection', schedule='every 5m', deliver='local',
                         script='gate.py', monitor_script='gate.py',
                         attach_to_session=False, model='chosen-coordinator')
        card = kb.create_task(board, title='Existing selected leaf', assignee='ang',
                              model_override='chosen-task', provider_override='chosen-provider')
        kb.add_notify_sub(board, task_id=card, platform='continuation', chat_id=job['id'],
                          notifier_profile='default', delivery_mode='wake', delivery_metadata={
            'authority_actor': 'Brian', 'authority_reference': 'test attributable request',
            'authority_mode': 'standing_coordination', 'authority_task_id': card,
            'authority_assignee': 'ang', 'authority_home': str(get_hermes_home()),
            'authority_job_id': job['id'], 'authority_paused': False, 'authority_revoked': False,
            'procedure': c.PROCEDURE})
        kb.complete_task(board, card)
        sub = kb.list_notify_subs(board, card)[0]
        assert c.admission_reason(board, sub, profile='default') == 'next_selection'


def test_standing_capacity_open_and_closed_obligation_survive_restart(board, standing):
    c, job, card, target = standing
    busy = new_task(board)
    kb.claim_task(board, busy)
    kb.complete_task(board, card)
    assert c.collect_wakeup(board, job_id=job['id'], profile='default') == {'wakeAgent': False}
    kb.complete_task(board, busy)
    with kb.connect_closing() as reopened:
        wake = c.collect_wakeup(reopened, job_id=job['id'], profile='default')
        args = dict(card=card, event=wake['event'], profile='default', job_id=job['id'], next_card=target)
        assert c.observe_pending_obligations(reopened, job_id=job['id'], profile='default',
                                             deadline_seconds=60)[0]['reason'] == 'next_selection'
        assert c.bind_next_selection(reopened, **args)
        kb.claim_task(reopened, target)
        kb._set_worker_pid(reopened, target, 12345)
        effect = kb.list_events(reopened, target)[-1].id
        assert c.acknowledge_next_selection(reopened, **args, effect_event=effect)
    assert c.observe_pending_obligations(board, job_id=job['id'], profile='default',
                                         deadline_seconds=60) == []
    assert not c.acknowledge_next_selection(board, **args, effect_event=effect + 1)


@pytest.mark.parametrize('finish', ['completed', 'blocked', 'failed'])
def test_standing_ack_after_target_run_ends(board, standing, finish):
    c, job, card, target = standing
    kb.complete_task(board, card)
    wake = c.collect_wakeup(board, job_id=job['id'], profile='default')
    args = dict(card=card, event=wake['event'], profile='default', job_id=job['id'], next_card=target)
    assert c.bind_next_selection(board, **args)
    claimed = kb.claim_task(board, target)
    kb._set_worker_pid(board, target, 12345)
    spawned = kb.list_events(board, target)[-1].id
    if finish == 'completed':
        kb.complete_task(board, target)
    elif finish == 'blocked':
        kb.block_task(board, target, reason='retained target blocker', kind='needs_input')
    else:
        kb._record_task_failure(board, target, 'worker crashed', outcome='crashed',
                                failure_limit=1, release_claim=True, end_run=True)
    assert kb.get_run(board, claimed.current_run_id).worker_pid is None
    with kb.connect_closing() as reopened:
        assert c.acknowledge_next_selection(reopened, **args, effect_event=spawned)
        assert c.acknowledge_next_selection(reopened, **args, effect_event=spawned)
        assert c.admission_reason(reopened, _standing_subscription(reopened, card),
                                  profile='default') is None
        # The finished target may itself legitimately request the next selection;
        # the acknowledged source must never reappear in that wake.
        payload = c.collect_wakeup(reopened, job_id=job['id'], profile='default')
        assert payload.get('card') != card
        assert all(item['card'] != card for item in payload.get('exceptions', []))
        assert kb.get_task(reopened, target).status == ('done' if finish == 'completed' else 'blocked')
