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


@pytest.mark.parametrize("gate", ["healthy", "before_deadline", "owner", "hold", "job", "inference"])
def test_semantic_escalation_gates_are_quiet(board, monkeypatch, gate):
    from cron import jobs
    from hermes_cli import kanban_continuation as continuation
    task = pending(board)
    payload = continuation.collect_wakeup(board, job_id="job1", profile="default")
    started = kb.list_notify_subs(board)[0]["delivery_metadata"]["reason_admitted_at:authentication_blocker"]
    job = dict(subscription_only=gate != "inference", provider="openai-codex", model="gpt-6.1-sol",
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


def new_task(conn, **kwargs):
    return kb.create_task(conn, title="Authorized work", assignee="ang",
                          subscription_only=True, model_override="gpt-6.1-sol",
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


def pending(conn):
    task = new_task(conn)
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
