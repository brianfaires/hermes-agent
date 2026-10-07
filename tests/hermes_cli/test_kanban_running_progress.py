"""Running progress is a warning, independent of heartbeat and retry authority."""
import os

from hermes_cli import kanban_db as kb


def test_dispatch_warns_once_without_reclaiming_live_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    now = [10_000]
    monkeypatch.setattr(kb.time, "time", lambda: now[0])
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="isolated progress regression", assignee="worker")
        assert kb.claim_task(conn, tid, ttl_seconds=7200)
        kb._set_worker_pid(conn, tid, os.getpid())
        before = kb.get_task(conn, tid)
        now[0] += 901
        kb.heartbeat_worker(conn, tid, note="still working")
        kb.add_comment(conn, tid, author="worker", body="tests are next")
        for _ in range(2):
            kb.dispatch_once(conn, max_spawn=0)
        warnings = [e for e in kb.list_events(conn, tid) if e.kind == "running_progress_warning"]
        assert len(warnings) == 1
        assert warnings[0].run_id == before.current_run_id
        after = kb.get_task(conn, tid)
        assert (after.status, after.claim_lock, after.worker_pid, after.current_run_id) == (
            before.status, before.claim_lock, before.worker_pid, before.current_run_id)
        assert not any(e.kind in {"crashed", "gave_up", "timed_out"} for e in kb.list_events(conn, tid))
    finally:
        conn.close()


# stdlib cases are collected by pytest too, and can run without optional pytest.
import concurrent.futures
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


class RunningProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kanban-progress-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "board.db"
        self.env = patch.dict(os.environ, {
            "HOME": str(self.root), "HERMES_HOME": str(self.root / "hermes"),
            "HERMES_KANBAN_DB": str(self.db),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = int(time.time())
        self.clock = patch.object(kb.time, "time", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.conn = kb.connect(self.db)
        self.addCleanup(self.conn.close)

    def launch(self, pid=None):
        tid = kb.create_task(self.conn, title="isolated test", assignee="default", max_retries=0)
        self.assertIsNone(kb.get_task(self.conn, tid).current_run_id)
        self.assertTrue(kb.claim_task(self.conn, tid, ttl_seconds=7200))
        self.assertIsNone(kb.get_task(self.conn, tid).worker_pid)
        kb._set_worker_pid(self.conn, tid, pid or os.getpid())
        return tid

    def warnings(self, tid):
        return [e for e in kb.list_events(self.conn, tid) if e.kind == "running_progress_warning"]

    def attach(self, tid, name="tests.txt", data=b"3 assertions passed\n", mtime=None):
        path = self.root / name
        path.write_bytes(data)
        os.utime(path, (self.now if mtime is None else mtime,) * 2)
        return kb.add_attachment(self.conn, tid, filename=name, stored_path=str(path), size=len(data))

    def test_boundary_heartbeat_comments_and_real_progress_episodes(self):
        tid = self.launch()
        before = kb.get_task(self.conn, tid)
        self.now += 899
        self.assertEqual(kb.observe_running_progress(self.conn), [])
        self.now += 1
        kb.heartbeat_worker(self.conn, tid, note="still working")
        kb.add_comment(self.conn, tid, author="default", body="tests passed (no receipt)")
        self.assertEqual(kb.observe_running_progress(self.conn), [tid])
        self.assertEqual(kb.observe_running_progress(self.conn), [])
        self.now += 20
        receipt = self.attach(tid)
        event = kb.list_events(self.conn, tid)[-1]
        self.assertEqual(event.run_id, before.current_run_id)
        self.assertEqual(event.payload["attachment_id"], receipt)
        self.assertTrue(event.payload["progress_evidence"])
        self.now += 899
        self.assertEqual(kb.observe_running_progress(self.conn), [])
        self.now += 1
        self.assertEqual(kb.observe_running_progress(self.conn), [tid])
        self.assertEqual(len(self.warnings(tid)), 2)
        after = kb.get_task(self.conn, tid)
        for field in ("status", "claim_lock", "worker_pid", "current_run_id", "consecutive_failures"):
            self.assertEqual(getattr(before, field), getattr(after, field))

    def test_missing_empty_old_and_repeated_paths_are_not_progress(self):
        tid = self.launch()
        self.attach(tid, "valid.txt")
        self.now += 900
        self.assertEqual(kb.observe_running_progress(self.conn), [tid])
        kb.add_attachment(self.conn, tid, filename="missing", stored_path=str(self.root / "missing"), size=10)
        self.attach(tid, "empty", data=b"")
        self.attach(tid, "old", mtime=self.now - 1000)
        # Even refreshing a registered path must not turn reattachment into progress.
        self.attach(tid, "valid.txt")
        self.now += 900
        self.assertEqual(kb.observe_running_progress(self.conn), [])
        self.assertEqual(len(self.warnings(tid)), 1)

    def test_only_matching_assigned_local_current_spawn_is_launched(self):
        for defect in ("queued", "claimed", "no_spawn", "foreign_host", "pid_mismatch", "assignment", "run_ended", "dead"):
            with self.subTest(defect=defect):
                tid = kb.create_task(self.conn, title=defect, assignee="default")
                if defect == "queued":
                    continue
                kb.claim_task(self.conn, tid, ttl_seconds=7200)
                if defect == "claimed":
                    continue
                kb._set_worker_pid(self.conn, tid, os.getpid())
                with kb.write_txn(self.conn):
                    if defect == "no_spawn":
                        self.conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='spawned'", (tid,))
                    elif defect == "foreign_host":
                        self.conn.execute("UPDATE tasks SET claim_lock='foreign:1' WHERE id=?", (tid,))
                        self.conn.execute("UPDATE task_runs SET claim_lock='foreign:1' WHERE task_id=?", (tid,))
                    elif defect == "pid_mismatch":
                        self.conn.execute("UPDATE task_runs SET worker_pid=1 WHERE task_id=?", (tid,))
                    elif defect == "assignment":
                        self.conn.execute("UPDATE tasks SET assignee='other' WHERE id=?", (tid,))
                    elif defect == "run_ended":
                        self.conn.execute("UPDATE task_runs SET ended_at=? WHERE task_id=?", (self.now, tid))
                    elif defect == "dead":
                        kb._append_event(self.conn, tid, "spawned", {"pid": -1}, run_id=kb.get_task(self.conn, tid).current_run_id)
                        self.conn.execute("UPDATE tasks SET worker_pid=-1 WHERE id=?", (tid,))
                        self.conn.execute("UPDATE task_runs SET worker_pid=-1 WHERE task_id=?", (tid,))
        self.now += 901
        self.assertEqual(kb.observe_running_progress(self.conn), [])

    def test_current_run_does_not_import_previous_receipts_or_warning(self):
        tid = self.launch()
        old_run = kb.get_task(self.conn, tid).current_run_id
        self.attach(tid, "previous-run.txt")
        self.now += 901
        self.assertEqual(kb.observe_running_progress(self.conn), [tid])
        # Controlled private-DB retry fixture, never reclaim/signal a live PID.
        with kb.write_txn(self.conn):
            kb._end_run(self.conn, tid, outcome="released", status="released")
            self.conn.execute("UPDATE tasks SET status='ready',claim_lock=NULL,worker_pid=NULL WHERE id=?", (tid,))
        self.assertTrue(kb.claim_task(self.conn, tid, ttl_seconds=7200))
        kb._set_worker_pid(self.conn, tid, os.getpid())
        self.assertNotEqual(kb.get_task(self.conn, tid).current_run_id, old_run)
        self.now += 901
        self.assertEqual(kb.observe_running_progress(self.conn), [tid])
        self.assertEqual(len(self.warnings(tid)), 2)
        self.assertIsNone(self.warnings(tid)[-1].payload["last_progress_at"])

    def test_concurrent_connections_and_process_restart_dedup(self):
        tid = self.launch()
        self.now += 901
        barrier = threading.Barrier(2)
        def observe():
            conn = kb.connect(self.db)
            try:
                barrier.wait(timeout=10)
                return kb.observe_running_progress(conn)
            finally:
                conn.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(observe) for _ in range(2)]
            self.assertEqual(sum(len(f.result(timeout=15)) for f in futures), 1)
        script = (
            "from unittest.mock import patch; from hermes_cli import kanban_db as k; "
            "c=k.connect(); "
            f"clock=patch.object(k.time,'time',return_value={self.now + 900}); clock.start(); "
            "assert k.observe_running_progress(c)==[]; c.close()"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.warnings(tid)), 1)

    def test_dispatch_renews_live_claim_and_dry_run_does_not_warn(self):
        tid = self.launch()
        self.now += 901
        kb.heartbeat_worker(self.conn, tid)
        with kb.write_txn(self.conn):
            self.conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?", (self.now - 1, tid))
            self.conn.execute("UPDATE task_runs SET claim_expires=? WHERE task_id=?", (self.now - 1, tid))
        kb.dispatch_once(self.conn, max_spawn=0, dry_run=True)
        self.assertEqual(self.warnings(tid), [])
        result = kb.dispatch_once(self.conn, max_spawn=0)
        self.assertEqual(result.progress_warned, [tid])
        self.assertEqual(result.reclaimed, 0)
        self.assertGreater(kb.get_task(self.conn, tid).claim_expires, self.now)
        self.assertEqual(kb.get_task(self.conn, tid).worker_pid, os.getpid())

    def test_native_blob_storage_resets_episode_with_actual_bytes(self):
        tid = self.launch()
        self.now += 901
        self.assertEqual(kb.observe_running_progress(self.conn), [tid])
        data = b"Isolated test result: assertions passed; ready for review.\n"
        aid = kb.store_attachment_bytes(self.conn, tid, "handoff.txt", data, uploaded_by="default")
        attachment = kb.get_attachment(self.conn, aid)
        stored = Path(attachment.stored_path).read_bytes()
        self.assertEqual(hashlib.sha256(stored).digest(), hashlib.sha256(data).digest())
        self.assertTrue(kb.list_events(self.conn, tid)[-1].payload["progress_evidence"])
        self.assertEqual(kb.observe_running_progress(self.conn), [])
        self.now += 900
        self.assertEqual(kb.observe_running_progress(self.conn), [tid])

    def test_warning_preserves_control_hold_and_duplicate_spawn_is_not_progress(self):
        tid = self.launch()
        with kb.write_txn(self.conn):
            kb._append_event(self.conn, tid, "blocked", {"kind": "needs_input", "recurrences": 1})
        self.assertTrue(kb.has_active_control_hold(self.conn, tid))
        self.now += 901
        before = dict(self.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        self.assertEqual(kb.observe_running_progress(self.conn), [tid])
        self.assertTrue(kb.has_active_control_hold(self.conn, tid))
        self.assertEqual(dict(self.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()), before)
        kb._set_worker_pid(self.conn, tid, os.getpid())
        self.now += 901
        self.assertEqual(kb.observe_running_progress(self.conn), [])
