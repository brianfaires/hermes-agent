"""Real profile ContextVars and cron files; no external credential sources."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest


class ProfileScopeQualification(unittest.IsolatedAsyncioTestCase):
    async def test_nested_scopes_propagate_to_thread_and_reset_on_exception(self):
        from gateway.run import _profile_runtime_scope
        from hermes_constants import get_hermes_home
        from agent.secret_scope import current_secret_scope, get_secret

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            homes = [root / name for name in ('outer', 'inner')]
            for home in homes:
                home.mkdir()
                (home / '.env').write_text('QUALIFICATION_TOKEN=' + home.name + '\n')
                (home / 'config.yaml').write_text('{}\n')
            original_home, original_scope = get_hermes_home(), current_secret_scope()
            original_env = dict(os.environ)
            def read():
                return get_hermes_home(), get_secret('QUALIFICATION_TOKEN')
            with _profile_runtime_scope(homes[0]):
                self.assertEqual(await asyncio.to_thread(read), (homes[0], 'outer'))
                with self.assertRaisesRegex(RuntimeError, 'fixture'):
                    with _profile_runtime_scope(homes[1]):
                        self.assertEqual(await asyncio.to_thread(read), (homes[1], 'inner'))
                        raise RuntimeError('fixture')
                self.assertEqual(read(), (homes[0], 'outer'))
            self.assertEqual(get_hermes_home(), original_home)
            self.assertIs(current_secret_scope(), original_scope)
            self.assertEqual(dict(os.environ), original_env)

    async def test_heartbeat_uses_active_home_and_explicit_store_then_restores(self):
        from gateway.run import _profile_runtime_scope
        from cron import jobs

        with tempfile.TemporaryDirectory() as tmp:
            outer, inner = Path(tmp) / 'outer', Path(tmp) / 'inner'
            outer.mkdir()
            inner.mkdir()
            with _profile_runtime_scope(outer):
                self.assertIsNone(jobs.get_ticker_heartbeat_age())
                jobs.record_ticker_heartbeat(success=True)
                saved = (outer / 'cron/ticker_heartbeat').read_bytes()
                with jobs.use_cron_store(inner):
                    self.assertIsNone(jobs.get_ticker_heartbeat_age())
                    jobs.record_ticker_heartbeat(success=False)
                    self.assertIsNotNone(jobs.get_ticker_heartbeat_age())
                    self.assertIsNone(jobs.get_ticker_success_age())
                self.assertIsNotNone(jobs.get_ticker_success_age())
                self.assertEqual((outer / 'cron/ticker_heartbeat').read_bytes(), saved)
            self.assertTrue((inner / 'cron/ticker_heartbeat').is_file())
            self.assertFalse((inner / 'cron/ticker_last_success').exists())


if __name__ == '__main__':
    unittest.main()
