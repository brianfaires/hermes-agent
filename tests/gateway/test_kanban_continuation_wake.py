"""Real notifier/cursor paths; only the outbound model transport is a sentinel."""
import asyncio
import time
from pathlib import Path

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.handled = []
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append(text)

    async def handle_message(self, event):
        self.handled.append(event)
        receipt = getattr(event, "_native_decision_receipt", None)
        if receipt is not None:
            receipt.set_result(True)


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    (home / "profiles" / "ang").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "board.db"))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    with kb.connect_closing() as conn:
        yield conn


def make_task(conn, *, context="existing"):
    tid = kb.create_task(conn, title="Scoped work " + "x" * 500, assignee="ang",
                         subscription_only=True, model_override="gpt-6.1-sol",
                         provider_override="openai-codex")
    kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                      notifier_profile="default", delivery_mode="wake", delivery_metadata={
                          "authority_actor": "Brian", "authority_reference": "test scoped approval",
                          "authority_expires_at": int(time.time()) + 600,
                          "authority_task_id": tid, "authority_assignee": "ang",
                          "procedure": "autonomous-work-continuation",
                          "continuation_context": context})
    return tid


def tick(monkeypatch, adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._active_profile_name = lambda: "default"
    real_sleep = asyncio.sleep

    async def sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    asyncio.run(runner._kanban_notifier_watcher(interval=1))


@pytest.mark.parametrize("kind", ["transient", "dependency", None])
def test_blocked_checks_never_inject_or_consume(board, monkeypatch, kind):
    tid = make_task(board)
    kb.block_task(board, tid, reason="intentional wait", kind=kind)
    before = kb.list_notify_subs(board)
    adapter = RecordingAdapter()
    for _ in range(3):
        tick(monkeypatch, adapter)
    assert adapter.handled == []
    assert adapter.sent == []
    assert kb.list_notify_subs(board) == before


def test_completed_minimal_event_delivered_once_across_restart(board, monkeypatch):
    tid = make_task(board)
    kb._record_task_failure(board, tid, "worker crashed", outcome="crashed", failure_limit=1)
    adapter = RecordingAdapter()
    tick(monkeypatch, adapter)
    assert len(adapter.handled) == 1
    event = adapter.handled[0]
    assert len(event.text) < 250
    assert tid in event.text and "autonomous-work-continuation" in event.text
    assert event.source.profile == "default"
    assert event.source.chat_id == "chat-1"
    assert event.source.chat_type == "dm"
    tick(monkeypatch, adapter)
    assert len(adapter.handled) == 1


def test_fresh_context_does_not_inject_existing_chat(board, monkeypatch):
    tid = make_task(board, context="fresh")
    kb.complete_task(board, tid, summary="done")
    adapter = RecordingAdapter()
    tick(monkeypatch, adapter)
    assert adapter.handled == []
    assert adapter.sent == []


def test_decision_once_comment_does_not_release_or_ack(board, monkeypatch):
    from hermes_cli.kanban_continuation import collect_wakeup, acknowledge_wakeup
    tid = make_task(board)
    kb.block_task(board, tid, reason="scoped decision", kind="needs_input")
    adapter = RecordingAdapter()
    tick(monkeypatch, adapter)
    assert len(adapter.handled) == 1
    assert "decision_required" in adapter.handled[0].text
    assert "no execution approval" in adapter.handled[0].text
    event = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
    kb.add_comment(board, tid, author="default", body="Attributed answer, not a control action")
    comment = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
    assert kb.has_active_control_hold(board, tid)
    assert kb.get_task(board, tid).status == "blocked"
    assert not acknowledge_wakeup(board, job_id="job1", profile="default", card=tid,
                                  event=event, reason="decision_required", effect_event=comment)
    assert collect_wakeup(board, job_id="job1", profile="default") == {"wakeAgent": False}
    tick(monkeypatch, adapter)
    assert len(adapter.handled) == 1
    assert adapter.sent == []


def test_decision_failed_delivery_retries_after_restart(board, monkeypatch):
    tid = make_task(board)
    kb.block_task(board, tid, reason="scoped decision", kind="needs_input")
    before = kb.list_notify_subs(board)

    class FailingAdapter(RecordingAdapter):
        async def handle_message(self, event):
            raise RuntimeError("busy or failed receiver")

    for _ in range(13):
        tick(monkeypatch, FailingAdapter())
        assert kb.list_notify_subs(board) == before
    adapter = RecordingAdapter()
    with kb.connect_closing() as reopened:
        assert kb.list_notify_subs(reopened) == before
    tick(monkeypatch, adapter)
    tick(monkeypatch, adapter)
    assert len(adapter.handled) == 1


def test_decision_fresh_context_stays_silent(board, monkeypatch):
    tid = make_task(board, context="fresh")
    kb.block_task(board, tid, reason="needs input", kind="needs_input")
    before = kb.list_notify_subs(board)
    adapter = RecordingAdapter()
    tick(monkeypatch, adapter)
    assert adapter.handled == []
    assert kb.list_notify_subs(board) == before


def native_escalation(board, tid, profile="default"):
    from cron.jobs import create_job, use_cron_store
    from hermes_cli import kanban_continuation as continuation
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    home = get_profile_dir(profile)
    token = set_hermes_home_override(str(home))
    try:
        with use_cron_store(home):
            job = create_job(prompt="Read admitted card; decision only, recheck gates.", schedule="every 5m",
                             script="gate.py", monitor_script="gate.py", deliver="local",
                             provider="openai-codex", model="gpt-6.1-sol", subscription_only=True,
                             attach_to_session=False)
            metadata = next(sub for sub in kb.list_notify_subs(board, tid)
                            if sub["platform"] == "telegram")["delivery_metadata"]
            metadata.update(decision_required=True, continuation_context="fresh")
            kb.add_notify_sub(board, task_id=tid, platform="continuation", chat_id=job["id"],
                              notifier_profile=profile, delivery_mode="wake", delivery_metadata=metadata)
            admitted = continuation.collect_wakeup(board, job_id=job["id"], profile=profile)
            items = continuation.observe_pending_obligations(
                board, job_id=job["id"], profile=profile, deadline_seconds=900)
            signal_args = dict(job_id=job["id"], profile=profile, card=tid, reason=admitted["reason"],
                               deadline_seconds=900, now=items[0]["deadline_at"],
                               coordinator_state="unavailable", owner_gate=lambda item: True)
            assert continuation.signal_pending_escalation(board, **signal_args) is not None
            escalation = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
            # Lost producer return: reread the native event, never emit twice.
            assert continuation.signal_pending_escalation(board, **signal_args) is None
            return job["id"], escalation
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("profile", ["default", "ang"])
@pytest.mark.parametrize("failure", ["raise", "no_receipt", "false_receipt"])
def test_native_escalation_retained_until_exact_decision_handler(board, monkeypatch, profile, failure):
    import gateway.wake as wake

    tid = make_task(board)
    assert kb.block_task(board, tid, reason="real owner decision", kind="needs_input")
    adapter = RecordingAdapter()
    tick(monkeypatch, adapter)
    assert len(adapter.handled) == 1
    job, escalation = native_escalation(board, tid, profile)
    monkeypatch.setattr(wake, "WAKE_TURN_TIMEOUT_SECONDS", 0.02)

    class FailingAdapter(RecordingAdapter):
        async def handle_message(self, event):
            if failure == "raise":
                raise RuntimeError("owner receiver unavailable")
            if failure == "false_receipt":
                event._native_decision_receipt.set_result(False)

    before = kb.list_notify_subs(board)
    for _ in range(13):
        tick(monkeypatch, FailingAdapter())
        assert kb.list_notify_subs(board) == before
    with kb.connect_closing() as reopened:
        assert kb.list_notify_subs(reopened) == before
    tick(monkeypatch, adapter)
    assert len(adapter.handled) == 2
    event = adapter.handled[-1]
    assert f"escalation {escalation}" in event.text
    assert "no execution approval" in event.text
    assert event.source.profile == "default" and event.source.chat_id == "chat-1"
    assert len(event.text) < 350
    tick(monkeypatch, adapter)
    assert len(adapter.handled) == 2
    assert kb.has_active_control_hold(board, tid)
    assert kb.get_task(board, tid).status == "blocked"
    origin = next(sub for sub in kb.list_notify_subs(board, tid) if sub["chat_id"] == job)
    assert not any(key.startswith("reason_ack_event:") for key in origin["delivery_metadata"])


@pytest.mark.parametrize("grant_change", [None, "missing_decision", "wrong_event"])
def test_capability_escalation_existing_recipient_requires_exact_explicit_grant(board, monkeypatch, grant_change):
    tid = make_task(board)
    assert kb.block_task(board, tid, reason="real stalled capability", kind="capability")
    held = board.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
    recipient = kb.list_notify_subs(board, tid)[0]
    metadata = recipient["delivery_metadata"]
    metadata.update(decision_required=True, capability_decision_event=held)
    kb.add_notify_sub(board, task_id=tid, platform="telegram", chat_id="chat-1",
                      notifier_profile="default", delivery_mode="wake", delivery_metadata=metadata)
    job, escalation = native_escalation(board, tid)
    if grant_change:
        recipient = next(sub for sub in kb.list_notify_subs(board, tid) if sub["platform"] == "telegram")
        metadata = recipient["delivery_metadata"]
        if grant_change == "missing_decision":
            del metadata["decision_required"]
        else:
            metadata["capability_decision_event"] = held + 1
        kb.add_notify_sub(board, task_id=tid, platform="telegram", chat_id="chat-1",
                          notifier_profile="default", delivery_mode="wake", delivery_metadata=metadata)
    before = kb.list_notify_subs(board)
    adapter = RecordingAdapter()
    tick(monkeypatch, adapter)
    tick(monkeypatch, adapter)
    if grant_change:
        assert adapter.handled == []
        assert kb.list_notify_subs(board) == before
    else:
        assert len(adapter.handled) == 1
        event = adapter.handled[0]
        assert f"escalation {escalation}" in event.text
        assert "no execution approval" in event.text
        assert event.source.profile == "default" and event.source.chat_id == "chat-1"
    assert kb.get_task(board, tid).status == "blocked"
    assert kb.has_active_control_hold(board, tid)
    origin = next(sub for sub in kb.list_notify_subs(board, tid) if sub["chat_id"] == job)
    assert not any(key.startswith("reason_ack_event:") for key in origin["delivery_metadata"])


@pytest.mark.parametrize("gate", ["origin_home", "origin_episode", "origin_grant", "origin_job", "recipient_grant", "recipient_policy", "running"])
def test_native_escalation_rechecks_origin_and_recipient_without_consuming(board, monkeypatch, gate):
    import json
    from cron.jobs import pause_job, use_cron_store
    from hermes_constants import get_hermes_home

    tid = make_task(board)
    assert kb.block_task(board, tid, reason="real owner decision", kind="needs_input")
    adapter = RecordingAdapter()
    tick(monkeypatch, adapter)
    job, escalation = native_escalation(board, tid)
    if gate in ("origin_home", "origin_episode"):
        payload = json.loads(board.execute("SELECT payload FROM task_events WHERE id = ?", (escalation,)).fetchone()[0])
        payload["profile_home" if gate == "origin_home" else "episode_event"] = "wrong" if gate == "origin_home" else escalation
        with kb.write_txn(board):
            board.execute("UPDATE task_events SET payload = ? WHERE id = ?", (json.dumps(payload), escalation))
    elif gate == "origin_job":
        with use_cron_store(get_hermes_home()):
            pause_job(job, reason="operator stop")
    elif gate in ("origin_grant", "recipient_grant"):
        platform = "continuation" if gate == "origin_grant" else "telegram"
        sub = next(sub for sub in kb.list_notify_subs(board, tid) if sub["platform"] == platform)
        metadata = sub["delivery_metadata"]
        metadata["authority_expires_at"] = int(time.time()) - 1
        kb.add_notify_sub(board, task_id=tid, platform=platform, chat_id=sub["chat_id"],
                          notifier_profile="default", delivery_mode="wake", delivery_metadata=metadata)
    elif gate == "recipient_policy":
        monkeypatch.setattr(GatewayRunner, "_kanban_policy_subscription", lambda self, sub: None)
    else:
        assert kb.unblock_task(board, tid)
        assert kb.claim_task(board, tid, claimer="live-owner")
    before = kb.list_notify_subs(board)
    tick(monkeypatch, adapter)
    tick(monkeypatch, adapter)
    assert len(adapter.handled) == 1
    assert kb.list_notify_subs(board) == before


def test_paused_escalation_cannot_be_consumed_with_earlier_blocked_delivery(board, monkeypatch):
    from cron.jobs import pause_job, resume_job, use_cron_store
    from hermes_constants import get_hermes_home

    tid = make_task(board)
    assert kb.block_task(board, tid, reason="real owner decision", kind="needs_input")
    # Leave the earlier blocked event undelivered. The escalation is a separate
    # decision obligation, not part of that earlier handler's receipt.
    job, escalation = native_escalation(board, tid)
    with use_cron_store(get_hermes_home()):
        pause_job(job, reason="operator pause")
    adapter = RecordingAdapter()
    tick(monkeypatch, adapter)
    assert len(adapter.handled) == 1
    recipient = next(sub for sub in kb.list_notify_subs(board, tid) if sub["platform"] == "telegram")
    assert recipient["last_event_id"] < escalation
    with use_cron_store(get_hermes_home()):
        resume_job(job)
    tick(monkeypatch, adapter)
    assert len(adapter.handled) == 2
    assert f"escalation {escalation}" in adapter.handled[-1].text
    tick(monkeypatch, adapter)
    assert len(adapter.handled) == 2


def test_policy_redirect_retains_native_decision_until_origin_handler(board, monkeypatch):
    from hermes_cli.kanban_notifications import NotifyTarget, policy_subscription
    import hermes_cli.kanban_notifications as notifications

    tid = make_task(board)
    assert kb.block_task(board, tid, reason="real owner decision", kind="needs_input")
    adapter = RecordingAdapter()
    tick(monkeypatch, adapter)
    job, escalation = native_escalation(board, tid)
    before = kb.list_notify_subs(board)
    monkeypatch.setattr(notifications, "telegram_home_target",
                        lambda **kwargs: NotifyTarget("telegram", "home-only"))
    monkeypatch.setattr(GatewayRunner, "_kanban_policy_subscription",
                        lambda self, sub: policy_subscription(
                            sub, cfg={"kanban": {"notification_policy": "telegram_home_only"}}))
    tick(monkeypatch, adapter)
    assert adapter.sent == []
    assert len(adapter.handled) == 1
    assert kb.list_notify_subs(board) == before
    monkeypatch.setattr(GatewayRunner, "_kanban_policy_subscription",
                        lambda self, sub: policy_subscription(
                            sub, cfg={"kanban": {"notification_policy": "origin"}}))
    tick(monkeypatch, adapter)
    assert len(adapter.handled) == 2
    assert adapter.handled[-1].source.chat_id == "chat-1"
    assert f"escalation {escalation}" in adapter.handled[-1].text
    tick(monkeypatch, adapter)
    assert len(adapter.handled) == 2
