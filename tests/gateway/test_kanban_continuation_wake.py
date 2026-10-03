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
