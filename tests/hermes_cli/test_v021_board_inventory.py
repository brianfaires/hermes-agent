"""Board inventory must describe disk boards independently of worker DB pins."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from hermes_cli import kanban, kanban_db as kb


class BoardInventoryTests(unittest.TestCase):
    def test_inventory_ignores_worker_pin_without_mutating_databases(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            'HOME': root, 'HERMES_HOME': root, 'HERMES_KANBAN_HOME': root,
            'HERMES_KANBAN_DB': '', 'HERMES_KANBAN_BOARD': 'default',
        }):
            paths = {'default': Path(root) / 'kanban.db',
                     'engineering': Path(root) / 'kanban/boards/engineering/kanban.db'}
            for slug, path in paths.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(path) as conn:
                    conn.execute('CREATE TABLE tasks(status TEXT)')
                    conn.execute('INSERT INTO tasks VALUES (?)', (slug,))
            pinned = Path(root) / 'worker.db'
            with sqlite3.connect(pinned) as conn:
                conn.execute('CREATE TABLE tasks(status TEXT)')
                conn.execute("INSERT INTO tasks VALUES ('wrong-board')")
            before = {p: p.read_bytes() for p in [*paths.values(), pinned]}
            with patch.dict(os.environ, {'HERMES_KANBAN_DB': str(pinned)}):
                parser = argparse.ArgumentParser()
                kanban.build_parser(parser.add_subparsers())
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(kanban.kanban_command(parser.parse_args(['kanban', 'boards', 'list', '--json'])), 0)
                inventory = {row['slug']: row for row in json.loads(out.getvalue())}
                for slug, path in paths.items():
                    self.assertEqual(inventory[slug]['db_path'], str(path))
                    self.assertEqual(inventory[slug]['counts'], {slug: 1})
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    kanban.kanban_command(parser.parse_args(['kanban', 'boards', 'list']))
                self.assertIn('HERMES_KANBAN_DB', out.getvalue())
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    kanban.kanban_command(parser.parse_args(['kanban', 'boards', 'show']))
                self.assertIn(f"DB path:      {paths['default']}", out.getvalue())
                self.assertIn('HERMES_KANBAN_DB', out.getvalue())
                self.assertEqual(kb.kanban_db_path('engineering'), pinned)
            self.assertEqual(before, {p: p.read_bytes() for p in before})


if __name__ == '__main__':
    unittest.main()
