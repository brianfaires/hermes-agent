"""Ordinary create/set/API paths share persistent-workspace metadata rules."""
import argparse
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from hermes_cli import kanban, kanban_db as kb
from plugins.kanban.dashboard import plugin_api as api


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'HERMES_HOME':str(self.root), 'HERMES_KANBAN_HOME':str(self.root), 'HERMES_KANBAN_DB':str(self.root/'kanban.db')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = kb.connect()
        self.addCleanup(self.conn.close)

    def test_create_set_normalize_and_reject_without_partial_writes(self):
        tid = kb.create_task(self.conn,title='dir',workspace_kind='dir',workspace_path=f' {self.root} ',branch_name=' feature/one ')
        self.assertEqual(kb.get_task(self.conn,tid).branch_name,'feature/one')
        kb.set_branch_name(self.conn,tid,' feature/two ')
        kb.set_workspace_path(self.conn,tid,f' {self.root}/next ')
        for bad in ['../bad','-option','bad..ref','a.lock','a b']:
            with self.subTest(branch=bad), self.assertRaises(ValueError):
                kb.set_branch_name(self.conn,tid,bad)
        with self.assertRaises(ValueError): kb.set_workspace_path(self.conn,tid,'relative')
        row = kb.get_task(self.conn,tid)
        self.assertEqual((row.branch_name,row.workspace_path),('feature/two',str(self.root/'next')))
        count = self.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
        for kwargs in [dict(workspace_kind='scratch',branch_name='feature/x'),dict(workspace_kind='dir'),dict(workspace_path='relative')]:
            with self.assertRaises(ValueError): kb.create_task(self.conn,title='bad',**kwargs)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],count)

    def test_dashboard_forwards_branch_and_real_db_rejects_invalid(self):
        result = api.create_task(api.CreateTaskBody(title='api',workspace_kind='dir',workspace_path=str(self.root),branch_name=' feature/api '),board='default')
        self.assertEqual(result['task']['branch_name'],'feature/api')
        with self.assertRaises(api.HTTPException) as error:
            api.create_task(api.CreateTaskBody(title='bad',workspace_kind='dir',workspace_path=str(self.root),branch_name='bad..ref'),board='default')
        self.assertEqual(error.exception.status_code,400)

    def test_cli_persistent_dir_branch_roundtrip(self):
        parser=argparse.ArgumentParser(); kanban.build_parser(parser.add_subparsers())
        args=parser.parse_args(['kanban','create','CLI','--workspace',f'dir:{self.root}','--branch','feature/cli'])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(kanban.kanban_command(args),0)
        self.assertEqual(self.conn.execute("SELECT branch_name FROM tasks WHERE title='CLI'").fetchone()[0],'feature/cli')
