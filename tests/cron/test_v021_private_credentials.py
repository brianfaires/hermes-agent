"""Private cron refresh and child scopes through real profile files/imports."""
import concurrent.futures
import contextvars
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent import secret_scope as secrets
from cron import scheduler
from hermes_cli import env_loader
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


class PrivateCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a, self.b = [self.root / x for x in ('a', 'b')]
        for home in (self.a, self.b):
            (home / 'scripts').mkdir(parents=True)
            (home / '.env').write_text(f'CUSTOM_SECRET={home.name}\nOPENAI_API_KEY={home.name}-key\nDISCORD_HOME_CHANNEL={home.name}-room\n')
        self.addCleanup(secrets.set_multiplex_active, secrets.is_multiplex_active())
        token = secrets.set_secret_scope(None)
        self.addCleanup(secrets.reset_secret_scope, token)
        secrets.set_multiplex_active(True)
        for name, value in (('_APPLIED_HOMES', set()), ('_SECRET_SOURCE_VALUES_BY_HOME', {}), ('_SECRET_SOURCES', {})):
            p = patch.object(env_loader, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_private_refresh_observes_edits_without_environment_writes(self):
        before = dict(os.environ)
        first = secrets.refresh_profile_secret_scope(self.a)
        (self.a / '.env').write_text('CUSTOM_SECRET=changed\n')
        second = secrets.refresh_profile_secret_scope(self.a)
        self.assertEqual(first['CUSTOM_SECRET'], 'a')
        self.assertEqual(second, {'CUSTOM_SECRET': 'changed'})
        self.assertIsNone(secrets.current_secret_scope())
        self.assertEqual(dict(os.environ), before)

    def test_external_refresh_is_per_home_and_op_bootstrap_private(self):
        (self.a / '.op.env').write_text('OP_SERVICE_ACCOUNT_TOKEN=a-bootstrap\n')
        env_loader._APPLIED_HOMES.add(str(self.b.resolve()))
        env_loader._SECRET_SOURCE_VALUES_BY_HOME[str(self.b.resolve())] = {'OTHER': 'b'}
        seen = []
        def apply(cfg, home, *, environ):
            seen.append(dict(environ))
            environ['CUSTOM_SECRET'] = 'resolved'
            return SimpleNamespace(sources=[object()], provenance={'CUSTOM_SECRET': SimpleNamespace(source='fixture')})
        before = dict(os.environ)
        with patch.object(env_loader, '_load_secrets_config', return_value={'sources': {}}), patch('agent.secret_sources.registry.apply_all', side_effect=apply):
            value = secrets.refresh_profile_secret_scope(self.a)
        self.assertEqual(value['CUSTOM_SECRET'], 'resolved')
        self.assertEqual(seen[0]['OP_SERVICE_ACCOUNT_TOKEN'], 'a-bootstrap')
        self.assertEqual(env_loader.get_secret_source_values(self.b), {'OTHER': 'b'})
        self.assertIn(str(self.b.resolve()), env_loader._APPLIED_HOMES)
        self.assertEqual(dict(os.environ), before)

    def test_single_profile_external_source_keeps_shell_bootstrap(self):
        secrets.set_multiplex_active(False)
        seen = []
        def apply(cfg, home, *, environ):
            seen.append(environ.get('OP_SERVICE_ACCOUNT_TOKEN'))
            return SimpleNamespace(sources=[], provenance={})
        with patch.dict(os.environ, {'OP_SERVICE_ACCOUNT_TOKEN': 'shell-bootstrap'}), patch.object(env_loader, '_load_secrets_config', return_value={'sources': {}}), patch('agent.secret_sources.registry.apply_all', side_effect=apply):
            secrets.refresh_profile_secret_scope(self.a)
            secrets.refresh_profile_secret_scope(self.b, inherit_process_secrets=False)
        self.assertEqual(seen, ['shell-bootstrap', None])

    def test_scoped_late_loader_does_not_mutate_process_or_dotenv(self):
        original = (self.a / '.env').read_bytes()
        before = dict(os.environ)
        secrets.set_secret_scope({'CUSTOM_SECRET': 'installed'})
        env_loader.load_hermes_dotenv(hermes_home=self.a)
        self.assertEqual(secrets.get_secret('CUSTOM_SECRET'), 'installed')
        self.assertEqual(dict(os.environ), before)
        self.assertEqual((self.a / '.env').read_bytes(), original)

    def test_child_scope_is_authoritative_and_unscoped_multiplex_refuses(self):
        base = {'PATH': '/bin', 'CUSTOM_SECRET': 'wrong', 'UNSEEN_SECRET': 'wrong', 'OPENAI_API_KEY': 'wrong'}
        with self.assertRaises(secrets.UnscopedSecretError):
            secrets.scoped_subprocess_environment(base)
        secrets.set_secret_scope({'CUSTOM_SECRET': 'right', 'PATH': 'profile-must-not-override'})
        self.assertEqual(secrets.scoped_subprocess_environment(base), {'PATH': '/bin', 'CUSTOM_SECRET': 'right'})
        self.assertEqual(base['CUSTOM_SECRET'], 'wrong')

    def test_single_profile_shell_credential_compatibility(self):
        secrets.set_multiplex_active(False)
        secrets.set_secret_scope({'CUSTOM_SECRET': 'profile'})
        with patch.dict(os.environ, {'SHELL_ONLY_KEY': 'exported'}):
            self.assertEqual(secrets.get_secret('SHELL_ONLY_KEY'), 'exported')
            env = secrets.scoped_subprocess_environment(os.environ)
        self.assertEqual(env['SHELL_ONLY_KEY'], 'exported')
        self.assertEqual(env['CUSTOM_SECRET'], 'profile')

    def test_direct_job_refresh_and_exception_restore_scope(self):
        home_token = set_hermes_home_override(self.a)
        self.addCleanup(reset_hermes_home_override, home_token)
        outer = {'CUSTOM_SECRET': 'outer'}
        secrets.set_secret_scope(outer)
        before = dict(os.environ)
        def fail(*args, **kwargs):
            self.assertEqual(secrets.get_secret('CUSTOM_SECRET'), 'a')
            env_loader.load_hermes_dotenv(hermes_home=self.a)
            raise ValueError('fixture failure')
        with patch.object(scheduler, '_run_job_scoped', side_effect=fail):
            with self.assertRaisesRegex(ValueError, 'fixture failure'):
                scheduler.run_job({'id': 'test'})
        self.assertIs(secrets.current_secret_scope(), outer)
        self.assertEqual(dict(os.environ), before)

    def test_real_script_jobs_share_executor_without_profile_bleed(self):
        script = 'import json, os\nprint(json.dumps({k: os.getenv(k) for k in ["CUSTOM_SECRET", "OPENAI_API_KEY", "HERMES_HOME", "UNSEEN_SECRET"]}))\n'
        for home in (self.a, self.b):
            (home / 'scripts' / 'fixture.py').write_text(script)
        before = dict(os.environ)
        barrier = threading.Barrier(2)
        def run(home):
            token = set_hermes_home_override(home)
            try:
                barrier.wait(timeout=10)
                result = scheduler.run_job({'id': 'fixture', 'no_agent': True, 'script': 'fixture.py'})
                self.assertTrue(result[0], result)
                self.assertIsNone(secrets.current_secret_scope())
                return json.loads(result[2])
            finally:
                reset_hermes_home_override(token)
        with patch.object(scheduler, '_parallel_pool', None), patch.object(scheduler, '_parallel_pool_max_workers', None):
            pool = scheduler._get_parallel_pool(2)
            try:
                self.assertIs(pool, scheduler._get_parallel_pool(2))
                futures = [pool.submit(contextvars.copy_context().run, run, home) for home in (self.a, self.b)]
                results = [future.result(timeout=30) for future in futures]
            finally:
                pool.shutdown(wait=True)
        for home, value in zip((self.a, self.b), results):
            self.assertEqual(value['CUSTOM_SECRET'], home.name)
            self.assertEqual(Path(value['HERMES_HOME']), home)
            self.assertIsNone(value['OPENAI_API_KEY'])
            self.assertIsNone(value['UNSEEN_SECRET'])
        self.assertEqual(dict(os.environ), before)

    def test_bot_chat_selects_receiver_credentials_and_rejects_missing_profile(self):
        from hermes_cli import profiles
        secrets.set_secret_scope(secrets.refresh_profile_secret_scope(self.a))
        captured = []
        def send(*args, **kwargs):
            captured.append(kwargs['env'])
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(profiles, 'get_profile_dir', return_value=self.b), patch.object(profiles, 'profile_exists', return_value=True), patch.object(scheduler.subprocess, 'run', side_effect=send):
            error = scheduler._deliver_to_bot_chat({'id': 'fixture'}, 'hello', 'b')
        self.assertIsNone(error)
        self.assertEqual(captured[0]['CUSTOM_SECRET'], 'b')
        self.assertEqual(captured[0]['OPENAI_API_KEY'], 'b-key')
        self.assertEqual(Path(captured[0]['HERMES_HOME']), self.b)
        self.assertEqual(secrets.get_secret('CUSTOM_SECRET'), 'a')
        with patch.object(profiles, 'profile_exists', return_value=False), patch.object(scheduler.subprocess, 'run') as send:
            self.assertIn('no longer exists', scheduler._deliver_to_bot_chat({'id': 'fixture'}, 'hello', 'missing'))
            send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
