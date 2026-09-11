"""Future worker keys cannot cross the delegated subprocess boundary."""
import json
import os
import subprocess
import sys
import unittest
from agent.delegation_context import delegated_child_context, delegated_child_subprocess_env

class ChildEnvironmentTests(unittest.TestCase):
    def test_unknown_keys_scrubbed_and_real_child_refuses_mutation(self):
        original = dict(os.environ, HERMES_KANBAN_FUTURE_TOKEN='sensitive', HERMES_KANBAN_DB='/not-used', SAFE_FIXTURE='kept')
        with delegated_child_context():
            env = delegated_child_subprocess_env(original)
        self.assertEqual(original['HERMES_KANBAN_FUTURE_TOKEN'], 'sensitive')
        self.assertFalse(any(key.startswith('HERMES_KANBAN_') for key in env))
        script = '''import os,json
from hermes_cli.kanban_db import _assert_not_delegated_child_mutation
try:
    _assert_not_delegated_child_mutation()
except PermissionError:
    print(json.dumps({'refused':True,'safe':os.environ['SAFE_FIXTURE'],'keys':[k for k in os.environ if k.startswith('HERMES_KANBAN_')]}))
else:
    raise AssertionError('child mutation guard allowed')
'''
        result = subprocess.run([sys.executable, '-B', '-c', script], env=env, text=True, capture_output=True, timeout=30, check=True)
        self.assertEqual(json.loads(result.stdout), {'refused': True, 'safe': 'kept', 'keys': []})
        self.assertEqual(delegated_child_subprocess_env(original), original)
