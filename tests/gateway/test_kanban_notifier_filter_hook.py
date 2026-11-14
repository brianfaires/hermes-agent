"""``pre_kanban_notify``: a plugin may suppress a claimed kanban event before delivery.

Suppressed events are never sent or woken, and the subscription cursor still moves past them so
they are not re-sent on the next tick. With no plugin registered, delivery is unchanged.
"""

import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import plugins


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append(text)

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


async def _run_one_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _subscribed_task(tmp_path, monkeypatch, *, block=False):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="child card", assignee="worker",
                             session_id="agent:main:telegram:dm:chat-1")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                           delivery_mode="notify+wake", delivery_metadata={"chat_type": "dm"})
        if block:
            kb.block_task(conn, tid, reason="needs a decision")
        else:
            kb.complete_task(conn, tid, summary="child finished")
        return tid
    finally:
        conn.close()


def _cursor(tid):
    conn = kbc.connect()
    try:
        row = conn.execute("SELECT last_event_id FROM kanban_notify_subs WHERE task_id = ?", (tid,)).fetchone()
        top = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()
        return int(row[0]), int(top[0])
    finally:
        conn.close()


def _install_hook(monkeypatch, callback):
    calls = []

    def invoke_hook(name, **kwargs):
        if name != "pre_kanban_notify":
            return []
        calls.append(kwargs)
        result = callback(**kwargs)
        return [] if result is None else [result]

    monkeypatch.setattr(plugins, "has_hook", lambda name: name == "pre_kanban_notify")
    monkeypatch.setattr(plugins, "invoke_hook", invoke_hook)
    return calls


def test_hook_is_a_valid_hook_name():
    assert "pre_kanban_notify" in plugins.VALID_HOOKS


def test_suppressed_completion_is_not_sent_or_woken_and_cursor_advances(tmp_path, monkeypatch):
    tid = _subscribed_task(tmp_path, monkeypatch)
    calls = _install_hook(monkeypatch, lambda event, **_: {"action": "suppress", "reason": "child"}
                          if event["kind"] == "completed" else None)
    adapter = RecordingAdapter()
    runner = _runner(adapter)

    asyncio.run(_run_one_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.handled == []
    cursor, top = _cursor(tid)
    assert cursor == top
    # A second tick finds nothing new: the suppressed event is not re-sent.
    runner._running = True
    asyncio.run(_run_one_tick(monkeypatch, runner))
    assert adapter.sent == []
    assert [c["event"]["kind"] for c in calls] == ["completed"]


def test_hook_receives_event_subscription_task_and_board(tmp_path, monkeypatch):
    tid = _subscribed_task(tmp_path, monkeypatch)
    calls = _install_hook(monkeypatch, lambda **_: None)

    asyncio.run(_run_one_tick(monkeypatch, _runner(RecordingAdapter())))

    (call,) = calls
    assert call["event"]["kind"] == "completed"
    assert call["event"]["task_id"] == tid
    assert call["subscription"]["task_id"] == tid
    assert call["subscription"]["chat_id"] == "chat-1"
    assert call["task"]["id"] == tid
    assert call["task"]["status"] == "done"
    assert call["board"]


def test_allowed_event_is_delivered_unchanged(tmp_path, monkeypatch):
    _subscribed_task(tmp_path, monkeypatch, block=True)
    _install_hook(monkeypatch, lambda event, **_: {"action": "suppress"} if event["kind"] == "completed" else None)
    adapter = RecordingAdapter()

    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1


def test_failing_hook_fails_open(tmp_path, monkeypatch):
    _subscribed_task(tmp_path, monkeypatch)

    def boom(**_):
        raise RuntimeError("plugin bug")

    _install_hook(monkeypatch, boom)
    adapter = RecordingAdapter()

    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))

    assert len(adapter.sent) == 1


def test_no_plugin_means_upstream_delivery(tmp_path, monkeypatch):
    _subscribed_task(tmp_path, monkeypatch)
    monkeypatch.setattr(plugins, "has_hook", lambda name: False)

    def must_not_run(*_a, **_k):
        raise AssertionError("invoke_hook called without a registered hook")

    monkeypatch.setattr(plugins, "invoke_hook", must_not_run)
    adapter = RecordingAdapter()

    asyncio.run(_run_one_tick(monkeypatch, _runner(adapter)))

    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1
