"""Native dispatcher + notifier acceptance with an isolated real exit-23 child.

These stdlib unittest cases are also retained for normal pytest collection.
No model, network adapter, live profile, or process signal is used.
"""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from gateway.authz_mixin import GatewayAuthorizationMixin
from gateway.config import Platform
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    supports_async_delivery = True

    def __init__(self):
        self.sent = []
        self.handled = []
        self.fail = False

    async def send(self, chat_id, text, metadata=None):
        if self.fail:
            raise RuntimeError("isolated delivery failure")
        self.sent.append((chat_id, text, metadata))

    async def handle_message(self, event):
        if self.fail:
            raise RuntimeError("isolated wake failure")
        self.handled.append(event)


class Runner(GatewayAuthorizationMixin, GatewayKanbanWatchersMixin):
    def _active_profile_name(self):
        return "default"


async def notifier_tick(runner):
    runner._running = True
    real_sleep = asyncio.sleep
    async def one_tick(delay):
        if delay != 5:
            runner._running = False
        await real_sleep(0)
    with patch.object(asyncio, "sleep", one_tick):
        await asyncio.wait_for(runner._kanban_notifier_watcher(interval=1), timeout=5)


class RunningProgressDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kanban-progress-delivery-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {
            "HOME": str(self.root), "HERMES_HOME": str(self.root / "hermes"),
            "HERMES_KANBAN_DB": str(self.root / "board.db"),
        })
        env.start()
        self.addCleanup(env.stop)
        self.conn = kb.connect()
        self.addCleanup(self.conn.close)
        self.adapter = RecordingAdapter()
        self.runner = Runner()
        self.runner.adapters = {Platform.TELEGRAM: self.adapter}
        self.runner._kanban_dispatcher_lock_handle = object()
        self.runner._launch_profile_home = self.root / "hermes"
        self.now = int(time.time())
        clock = patch.object(kb.time, "time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def subscribe(self, tid, mode):
        kb.add_notify_sub(
            self.conn, task_id=tid, platform="telegram", chat_id="isolated-coordinator",
            thread_id="17", user_id="isolated-user", chat_type="dm",
            notifier_profile="default", delivery_mode=mode,
        )

    def test_actual_dispatch_child_warning_return_path_and_nonzero_crash_no_repeat(self):
        tid = kb.create_task(self.conn, title="isolated worker", assignee="default", max_retries=0)
        self.subscribe(tid, "notify+wake")
        self.assertIsNone(kb.get_task(self.conn, tid).current_run_id)
        children = []
        def spawn(task, workspace_path, board):
            # The spawn callback sees a durable assignment and claim, not a launch.
            current = kb.get_task(self.conn, task.id)
            self.assertEqual(current.assignee, "default")
            self.assertIsNotNone(current.current_run_id)
            self.assertIsNone(current.worker_pid)
            child = subprocess.Popen([sys.executable, "-c",
                "import threading,sys; done=threading.Event(); "
                "threading.Thread(target=lambda:(sys.stdin.read(1),done.set()),daemon=True).start(); "
                "done.wait(30); sys.exit(23)"],
                stdin=subprocess.PIPE)
            children.append(child)
            return child.pid
        try:
            result = kb.dispatch_once(self.conn, spawn_fn=spawn, max_spawn=1)
            self.assertEqual([s[0] for s in result.spawned], [tid])
            child = children[0]
            launched = kb.get_task(self.conn, tid)
            self.assertEqual(launched.worker_pid, child.pid)
            self.assertTrue(kb._pid_alive(child.pid))
            spawn_event = [e for e in kb.list_events(self.conn, tid) if e.kind == "spawned"][-1]
            self.assertEqual(spawn_event.run_id, launched.current_run_id)
            asyncio.run(notifier_tick(self.runner))
            self.assertEqual(self.adapter.sent, [])
            self.now += 901
            kb.heartbeat_worker(self.conn, tid, note="alive, no artifact")
            self.assertEqual(kb.dispatch_once(self.conn, max_spawn=0).progress_warned, [tid])
            asyncio.run(notifier_tick(self.runner))
            self.assertEqual(len(self.adapter.sent), 1)
            self.assertIn("WARNING", self.adapter.sent[0][1])
            self.assertIn("not proof of failure", self.adapter.sent[0][1])
            self.assertEqual(len(self.adapter.handled), 1)
            wake = self.adapter.handled[0]
            self.assertEqual((wake.source.chat_id, wake.source.thread_id, wake.source.chat_type),
                             ("isolated-coordinator", "17", "dm"))
            self.assertIn("no stop/retry/release authority", wake.text)
            kb.dispatch_once(self.conn, max_spawn=0)
            asyncio.run(notifier_tick(self.runner))
            self.assertEqual(len(self.adapter.sent), 1)
            self.assertEqual(kb.get_task(self.conn, tid).claim_lock, launched.claim_lock)
            # EOF makes the child exit 23 itself. Let the native reaper retain
            # wait status, so existing crash machinery proves nonzero exit.
            child.stdin.close()
            deadline = time.monotonic() + 10
            while kb._pid_alive(child.pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(kb._pid_alive(child.pid))
            if os.name == "nt":
                # Windows has no waitpid reap registry; still verify the real
                # nonzero exit before exercising native dead-PID recovery.
                self.assertEqual(child.wait(timeout=5), 23)
            result = kb.dispatch_once(self.conn, max_spawn=0)
            self.assertEqual(result.crashed, [tid])
            if os.name != "nt":
                self.assertEqual(kb._classify_worker_exit(child.pid), ("nonzero_exit", 23))
            child.returncode = 23  # already reaped by the native dispatcher
            asyncio.run(notifier_tick(self.runner))
            crash_messages = len(self.adapter.sent)
            self.assertGreater(crash_messages, 1)
            self.assertTrue(any("crashed" in m[1] for m in self.adapter.sent))
            self.assertEqual(kb.dispatch_once(self.conn, max_spawn=0).crashed, [])
            asyncio.run(notifier_tick(self.runner))
            self.assertEqual(len(self.adapter.sent), crash_messages)
            events = kb.list_events(self.conn, tid)
            self.assertEqual(sum(e.kind == "crashed" for e in events), 1)
            self.assertEqual(sum(e.kind == "running_progress_warning" for e in events), 1)
        finally:
            for child in children:
                if not child.stdin.closed:
                    child.stdin.close()
                child.wait(timeout=35)

    def test_warning_delivery_failure_retries_then_dedups_notify_and_wake(self):
        for mode in ("notify", "wake"):
            with self.subTest(mode=mode):
                tid = kb.create_task(self.conn, title=mode, assignee="default")
                self.subscribe(tid, mode)
                kb.claim_task(self.conn, tid, ttl_seconds=7200)
                kb._set_worker_pid(self.conn, tid, os.getpid())
                self.now += 901
                kb.observe_running_progress(self.conn)
                self.adapter.fail = True
                asyncio.run(notifier_tick(self.runner))
                old_sent, old_handled = len(self.adapter.sent), len(self.adapter.handled)
                self.adapter.fail = False
                asyncio.run(notifier_tick(self.runner))
                self.assertEqual(len(self.adapter.sent) - old_sent, int(mode == "notify"))
                self.assertEqual(len(self.adapter.handled) - old_handled, int(mode == "wake"))
                asyncio.run(notifier_tick(self.runner))
                self.assertEqual(len(self.adapter.sent) - old_sent, int(mode == "notify"))
                self.assertEqual(len(self.adapter.handled) - old_handled, int(mode == "wake"))
