"""Keep inherited OS user-session context without leaking profile credentials."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from agent import secret_scope as secrets
from cron import scheduler
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from tools.environments.local import build_subprocess_env

BUS = {'XDG_RUNTIME_DIR': '/run/user/1000',
       'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus'}
KEY_SETS = [(), ('XDG_RUNTIME_DIR',), ('DBUS_SESSION_BUS_ADDRESS',), tuple(BUS)]

class BusContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.before_mode = secrets.is_multiplex_active()
        self.addCleanup(secrets.set_multiplex_active, self.before_mode)
        token = secrets.set_secret_scope({})
        self.addCleanup(secrets.reset_secret_scope, token)
        home_token = set_hermes_home_override(self.home)
        self.addCleanup(reset_hermes_home_override, home_token)
        original = dict(os.environ)
        self.addCleanup(self.restore_environment, original)
        self.restore_environment({'PATH': '/usr/bin:/bin', 'HOME': str(self.home),
                                  'HERMES_HOME': str(self.home), 'LANG': 'C.UTF-8'})

    @staticmethod
    def restore_environment(values):
        os.environ.clear()
        os.environ.update(values)

    def check_child(self, multiplex, keys):
        secrets.set_multiplex_active(multiplex)
        os.environ.update({k: BUS[k] for k in keys})
        os.environ.update({'UNSEEN_SECRET': 'foreign-value', 'OPENAI_API_KEY': 'foreign-provider',
                           'XDG_UNLISTED': 'foreign-xdg', 'DBUS_UNLISTED': 'foreign-dbus'})
        secrets.set_secret_scope({'CUSTOM_SECRET': 'owned-value', 'OPENAI_API_KEY': 'owned-provider'})
        names = list(BUS) + ['UNSEEN_SECRET', 'OPENAI_API_KEY', 'CUSTOM_SECRET',
                            'XDG_UNLISTED', 'DBUS_UNLISTED', 'HERMES_HOME']
        scripts = self.home / 'scripts'
        scripts.mkdir()
        (scripts / 'probe.py').write_text('import os, json\nprint(json.dumps({k: os.getenv(k) for k in ' + repr(names) + '}))\n')
        before = dict(os.environ)
        success, output = scheduler._run_job_script('probe.py')
        self.assertTrue(success, output)
        observed = json.loads(output)
        for key, value in BUS.items():
            self.assertEqual(observed[key], value if key in keys else None)
        self.assertEqual(observed['CUSTOM_SECRET'], 'owned-value')
        self.assertIsNone(observed['OPENAI_API_KEY'])
        for key in ('UNSEEN_SECRET', 'XDG_UNLISTED', 'DBUS_UNLISTED'):
            self.assertEqual(observed[key], None if multiplex else before[key])
        self.assertEqual(Path(observed['HERMES_HOME']), self.home)
        self.assertEqual(dict(os.environ), before)

    def check_override(self, multiplex):
        secrets.set_multiplex_active(multiplex)
        os.environ.update(BUS)
        secrets.set_secret_scope({key: 'profile-override' for key in BUS})
        result = secrets.scoped_subprocess_environment(os.environ)
        for key, value in BUS.items():
            self.assertEqual(result[key], value)
            self.assertEqual(secrets.get_secret(key), value)

    def check_absent(self, multiplex):
        secrets.set_multiplex_active(multiplex)
        secrets.set_secret_scope({key: 'profile-only' for key in BUS})
        result = build_subprocess_env(secrets.scoped_subprocess_environment(os.environ))
        for key in BUS:
            self.assertNotIn(key, result)
            self.assertIsNone(secrets.get_secret(key))

    def test_multiplex_without_scope_fails_closed(self):
        secrets.set_multiplex_active(True)
        secrets.set_secret_scope(None)
        with self.assertRaises(secrets.UnscopedSecretError):
            secrets.scoped_subprocess_environment(BUS)

    def test_single_profile_without_scope_preserves_contract(self):
        secrets.set_multiplex_active(False)
        secrets.set_secret_scope(None)
        base = dict(BUS, UNSEEN_SECRET='single-profile')
        self.assertEqual(secrets.scoped_subprocess_environment(base), base)

    def test_exact_names_not_prefixes(self):
        for name in BUS:
            self.assertTrue(secrets._is_global_env(name))
        for name in ('XDG_UNLISTED', 'DBUS_UNLISTED', 'XDG_RUNTIME_DIR_SECRET',
                     'DBUS_SESSION_BUS_ADDRESS_TOKEN', 'OPENAI_API_KEY', 'UNSEEN_SECRET'):
            self.assertFalse(secrets._is_global_env(name))

for multiplex in (False, True):
    for index, keys in enumerate(KEY_SETS):
        def _check_child(self, mode=multiplex, selected=keys):
            self.check_child(mode, selected)
        setattr(BusContextTests, f'test_real_child_multiplex_{multiplex}_context_{index}', _check_child)
    for label, check in [('override', BusContextTests.check_override), ('absent', BusContextTests.check_absent)]:
        def _check_global(self, mode=multiplex, method=check):
            method(self, mode)
        setattr(BusContextTests, f'test_global_{label}_multiplex_{multiplex}', _check_global)

if __name__ == '__main__':
    unittest.main(verbosity=2)
