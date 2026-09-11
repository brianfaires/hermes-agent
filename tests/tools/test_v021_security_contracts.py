"""Security decisions use executable positions and complete scanner findings."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.process_guard import is_process_killer
from scripts import run_tests_parallel as runner
from tools import tirith_security as tirith


class ProcessClassifierTests(unittest.TestCase):
    def test_wrapped_killers_and_full_python_patterns_are_blocked(self):
        blocked = [
            ['pkill', '-f', 'hermes'], ['pkill', '--full', 'python'],
            ['pkill', '-if', 'python'], ['taskkill', '/IM', 'hermes.exe'],
            ['sudo', '-u', 'root', '--', '/usr/bin/pkill', '--full', 'python'],
            ['env', '-u', 'SOME_KEY', 'X=1', 'pkill', '-f', 'gateway'],
            ['timeout', '-s', 'TERM', '5s', 'env', '--unset', 'KEY', 'killall', 'hermes'],
            ['nice', '-n', '5', 'ionice', '-c', '3', 'pkill', '-f', 'hermes'],
            ['stdbuf', '-oL', 'nohup', 'setsid', 'pkill', '--full', 'python'],
            ['flock', '-w', '5', '/tmp/fixture.lock', 'pkill', '-f', 'hermes'],
            ['xargs', '-I', '{}', 'pkill', '-f', 'hermes'],
            ['flock', '/tmp/fixture.lock', '-c', 'pkill -f hermes'],
            ['bash', '-lc', 'echo harmless; env -u KEY pkill --full python'],
            'echo harmless && timeout 5 pkill -f hermes',
            ['env', '-S', 'pkill -f hermes'],
        ]
        for command in blocked:
            with self.subTest(command=command):
                self.assertTrue(is_process_killer(command))

    def test_harmless_operands_are_not_executables(self):
        allowed = [
            ['cat', '/tmp/hermes/skill'], ['echo', 'pkill', '-f', 'hermes'],
            ['env', '-u', 'KEY', 'cat', '/tmp/hermes/skill'],
            ['timeout', '5', 'cat', '/tmp/hermes/skill'],
            ['sudo', '-u', 'root', 'echo', 'pkill', '-f', 'hermes'],
            ['bash', '-lc', "echo 'pkill -f hermes'"],
            'echo pkill -f hermes', ['python', '-c', "print('pkill -f hermes')"],
            ['pkill', 'unrelated-fixture'], ['pkill', 'python'], None,
        ]
        for command in allowed:
            with self.subTest(command=command):
                self.assertFalse(is_process_killer(command))


class TirithFindingsTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(Path(tirith.__file__).resolve(), Path(__file__).resolve().parents[2] / 'tools' / 'tirith_security.py')

    def check(self, findings, code=2, raw=None):
        cfg = {'tirith_enabled':True, 'tirith_path':'/fixture/tirith', 'tirith_timeout':1, 'tirith_fail_open':False}
        result = SimpleNamespace(returncode=code, stdout=json.dumps({'findings':findings}) if raw is None else raw)
        with patch.object(tirith, '_load_security_config', return_value=cfg), patch.object(tirith, '_circuit_open', False), patch.object(tirith, 'is_platform_supported', return_value=True), patch.object(tirith, '_resolve_tirith_path', return_value='/fixture/tirith'), patch.object(tirith.subprocess, 'run', return_value=result):
            return tirith.check_command_security('echo fixture')

    def exact(self, left='pytest', right='pytest'):
        return {'rule_id':'threat_package_similar_name', 'description':f"Package ''{left}' in pypi is within edit distance 1 of popular package '{right}'. This could indicate a typosquatting attempt."}

    def test_exact_pair_warn_only_and_distinct_names_preserved(self):
        self.assertEqual(self.check([self.exact()]), {'action':'allow','findings':[],'summary':''})
        for left,right in [('PyTest','pytest'),('lodash.get','lodash-get')]:
            finding=self.exact(left,right)
            self.assertEqual(self.check([finding])['findings'],[finding])
            self.assertEqual(self.check([finding])['action'],'warn')
        blocked = self.check([self.exact()], code=1)
        self.assertEqual(blocked['action'],'block')
        self.assertEqual(blocked['findings'],[self.exact()])

    def test_late_nonsuppressible_finding_cannot_be_hidden_by_cap(self):
        danger={'rule_id':'actual-threat','description':'retain'}
        for benign in (self.exact(), {'rule_id':'lookalike_tld','value':'.app'}):
            result=self.check([benign] * (tirith._MAX_FINDINGS+1)+[danger])
            self.assertEqual(result['action'],'warn')
            self.assertEqual(result['findings'], [danger] if benign == self.exact() else [benign] * tirith._MAX_FINDINGS)
        mixed=[{'rule_id':'lookalike_tld','value':'.app'},danger]
        self.assertEqual(self.check(mixed)['findings'],mixed)
        many=[danger] * (tirith._MAX_FINDINGS+5)
        self.assertEqual(len(self.check(many)['findings']),tirith._MAX_FINDINGS)

    def test_malformed_findings_never_suppress_verdict(self):
        for raw in ('[]', 'null', '{"findings":{}}', '{"findings":[null]}', 'not json'):
            for code,action in ((1,'block'),(2,'warn')):
                with self.subTest(raw=raw,code=code):
                    self.assertEqual(self.check([],code=code,raw=raw)['action'],action)
        for finding in ({'rule_id':'lookalike_tld','value':['.app']}, {'rule_id':'threat_package_similar_name','description':{}}, self.exact() | {'description': self.exact()['description']+' unrelated warning'}):
            self.assertEqual(self.check([finding])['action'],'warn')


class ThreadCeilingTests(unittest.TestCase):
    def test_real_child_receives_ceiling_without_mutating_controller(self):
        keys=('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS','VECLIB_MAXIMUM_THREADS')
        real_popen=subprocess.Popen
        child_code='import os,json; print(json.dumps({k:os.environ[k] for k in '+repr(keys)+'}))'
        captured=[]
        def harmless_child(command, **kwargs):
            captured.append((command,kwargs['env'].copy()))
            return real_popen([sys.executable,'-B','-c',child_code],**kwargs)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{k:'96' for k in keys}), patch.object(runner.subprocess,'Popen',side_effect=harmless_child), patch.object(runner,'_kill_tree') as killer:
            result=runner._run_one_file_once(Path('fixture.py'),[],Path(tmp),10)
            self.assertEqual(json.loads(result[2]),{k:'1' for k in keys})
            self.assertTrue(all(os.environ[k]=='96' for k in keys))
            self.assertFalse(Path(captured[0][1]['PYTEST_DEBUG_TEMPROOT']).exists())
            self.assertEqual(captured[0][0][1:3],['-m','pytest'])
            killer.assert_called_once()  # intercepted, no real signal/kill command
