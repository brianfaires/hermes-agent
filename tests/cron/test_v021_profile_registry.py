"""Profile-qualified cron lifecycle through real stores; no model or transport."""
import concurrent.futures
import contextlib
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from cron import executions, jobs, scheduler as sched
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


@contextlib.contextmanager
def profile(home):
    token = set_hermes_home_override(home)
    try:
        with jobs.use_cron_store(home):
            yield
    finally:
        reset_hermes_home_override(token)


class ProfileRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a, self.b = (Path(self.tmp.name) / name for name in ('a', 'b'))
        for field in ('_running_job_ids', '_running_since', '_running_futures',
                      '_running_fire_owners', '_interrupted_job_ids'):
            value = {} if field in ('_running_since', '_running_futures', '_running_fire_owners') else set()
            p = patch.object(sched, field, value)
            p.start()
            self.addCleanup(p.stop)

    def seed(self, home):
        with profile(home):
            jobs.save_jobs([{'id': 'same', 'name': 'fixture', 'prompt': 'Summarize fixture.',
                'schedule': {'kind': 'interval', 'minutes': 5}, 'enabled': True,
                'next_run_at': '2020-01-01T00:00:00+00:00', 'deliver': 'local'}])

    def test_equal_ids_local_precheck_global_count_and_captured_release(self):
        with profile(self.a):
            self.assertTrue(sched.try_register_running_job('same'))
            self.assertFalse(sched.try_register_running_job('same'))
        with profile(self.b):
            self.assertEqual(sched.get_running_job_ids(current_profile_only=True), set())
            self.assertTrue(sched.try_register_running_job('same'))
        self.assertEqual(sched.get_running_job_keys(), {(self.a, 'same'), (self.b, 'same')})
        sched.release_running_job('same', profile_home=self.a)
        self.assertEqual(sched.get_running_job_keys(), {(self.b, 'same')})
        with profile(self.b):
            self.assertEqual(sched.get_running_job_ids(current_profile_only=True), {'same'})
            sched.release_running_job('same')
        self.assertFalse(sched.get_running_job_keys())

    def test_real_tick_releases_after_copied_context_exits(self):
        callbacks = []
        class DeferredPool:
            def submit(self, callback):
                future = concurrent.futures.Future()
                callbacks.append((callback, future))
                return future
        for home in (self.a, self.b):
            self.seed(home)
        seen = []
        def body(job, **kwargs):
            seen.append(jobs._current_cron_store().cron_dir.parent)
            self.assertEqual(len(sched.get_running_job_keys()), 2 if len(seen) == 1 else 1)
            return True
        with patch.object(sched, '_get_parallel_pool', return_value=DeferredPool()), \
             patch.object(sched, '_maybe_run_worktree_maintenance'), \
             patch.object(sched, '_should_yield_tick_to_fresh_gateway', return_value=None), \
             patch('tools.mcp_tool._kill_orphaned_mcp_children'), \
             patch.object(sched, '_run_with_fire_claim_heartbeat', side_effect=lambda j, fn: fn(None)), \
             patch.object(sched, '_run_one_job_body', side_effect=body):
            for home in (self.a, self.b):
                with profile(home):
                    self.assertEqual(sched.tick(verbose=False, sync=False), 1)
            # Callbacks execute from the outer context, just like pool workers.
            for callback, future in callbacks:
                future.set_result(callback())
        self.assertEqual(seen, [self.a, self.b])
        self.assertFalse(sched.get_running_job_keys())
        self.assertFalse(sched._running_futures)

    def test_stale_sweep_uses_only_own_store_and_preserves_live_future(self):
        for home in (self.a, self.b):
            with profile(home):
                sched.try_register_running_job('same')
        # Explicit store overrides work even without changing the runtime home.
        with jobs.use_cron_store(self.b):
            row = executions.create_execution('same', source='fixture')
            executions.finish_execution(row['id'], success=True)
            self.assertTrue((self.b / 'cron/executions.db').exists())
        with profile(self.a):
            self.assertEqual(sched.sweep_stale_inflight([]), [])
        with profile(self.b):
            live = concurrent.futures.Future()
            sched._running_futures[(self.b, 'same')] = live
            self.assertEqual(sched.sweep_stale_inflight([]), [])
            live.set_result(True)
            self.assertEqual(sched.sweep_stale_inflight([]), ['same'])
        self.assertEqual(sched.get_running_job_keys(), {(self.a, 'same')})

    def test_shutdown_targets_exact_owner_and_profile(self):
        claims, tokens = {}, {}
        for home in (self.a, self.b):
            self.seed(home)
            with profile(home):
                sched.try_register_running_job('same')
                claims[home] = jobs.claim_job_for_fire('same', return_job=True)
        def body(job, execution_token, **kwargs):
            home = jobs._current_cron_store().cron_dir.parent
            tokens[home] = execution_token
            if home == self.a:
                with profile(self.b):
                    sched.run_one_job(claims[self.b])
                self.assertFalse(sched._is_interrupted('same', execution_token))
            else:
                owner = claims[self.b]['fire_claim']['by']
                self.assertEqual(sched.mark_running_jobs_interrupted('fixture stop', only_owners={('same', owner)}), ['same'])
                self.assertTrue(sched._consume_interrupted_flag('same', execution_token))
                self.assertEqual(jobs.get_job('same')['last_status'], 'error')
            return True
        with patch.object(sched, '_run_with_fire_claim_heartbeat', side_effect=lambda j, fn: fn(None)), \
             patch.object(sched, '_run_one_job_body', side_effect=body), profile(self.a):
            sched.run_one_job(claims[self.a])
            self.assertNotEqual(jobs.get_job('same').get('last_status'), 'error')
        self.assertFalse(sched._running_fire_owners)

    def test_manual_run_dedupe_is_profile_local_and_always_releases(self):
        from tools import cronjob_tools
        self.seed(self.b)
        with profile(self.a):
            sched.try_register_running_job('same')
        with profile(self.b):
            claim = jobs.claim_job_for_fire('same', return_job=True)
            def execute(job, **kwargs):
                self.assertEqual(len(sched.get_running_job_keys()), 2)
                jobs.mark_job_run('same', True, expected_fire_owner=job['fire_claim']['by'])
                return True
            with patch.object(sched, 'run_one_job', side_effect=execute):
                self.assertTrue(cronjob_tools._run_claimed_job(claim)['success'])
            self.assertEqual(sched.get_running_job_ids(current_profile_only=True), set())
        self.assertEqual(sched.get_running_job_keys(), {(self.a, 'same')})

    def test_due_scan_recovers_own_recurring_error_despite_other_profile_run(self):
        now = datetime.now(timezone.utc)
        for home in (self.a, self.b):
            self.seed(home)
            with profile(home):
                jobs.update_job('same', {
                    'last_status': 'error',
                    'last_run_at': (now - timedelta(hours=3)).isoformat(),
                    'next_run_at': (now + timedelta(minutes=5)).isoformat(),
                })
        with profile(self.a):
            sched.try_register_running_job('same')
            self.assertEqual(jobs.get_due_jobs(), [])
        with profile(self.b):
            self.assertEqual([j['id'] for j in jobs.get_due_jobs()], ['same'])

    def test_due_scan_retires_own_exhausted_oneshot_without_deleting_live_peer(self):
        due = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        for home in (self.a, self.b):
            with profile(home):
                jobs.save_jobs([{'id': 'same', 'name': 'fixture', 'prompt': 'fixture',
                    'schedule': {'kind': 'once', 'run_at': due}, 'enabled': True,
                    'next_run_at': due, 'repeat': {'times': 1, 'completed': 1},
                    'deliver': 'local'}])
        with profile(self.a):
            sched.try_register_running_job('same')
            self.assertEqual(jobs.get_due_jobs(), [])
            self.assertIsNotNone(jobs.get_job('same'))
        with profile(self.b):
            self.assertEqual(jobs.get_due_jobs(), [])
            self.assertIsNone(jobs.get_job('same'))

    def test_ownerless_interrupt_does_not_cross_profiles_or_poison_next_claim(self):
        with profile(self.a):
            sched.try_register_running_job('same')
        sched.mark_running_jobs_interrupted('fixture shutdown')
        with profile(self.b):
            self.assertFalse(sched._is_interrupted('same'))
        with profile(self.a):
            self.assertTrue(sched._is_interrupted('same'))
            sched.release_running_job('same')
            sched.try_register_running_job('same')
            self.assertFalse(sched._is_interrupted('same'))


if __name__ == '__main__':
    unittest.main()
