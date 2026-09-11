"""Stdlib regressions: worktree exclusions preserve aggressive cleanup."""
import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


def entry(path, category='test'):
    return {'path': str(path), 'category': category, 'size': 15,
            'timestamp': (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()}


class WorktreeExclusions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cleanup-regression-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir()
        path = Path(__file__).resolve().parents[2] / 'plugins/disk-cleanup/disk_cleanup.py'
        spec = importlib.util.spec_from_file_location('worktree_cleanup_test_lib', path)
        assert spec is not None and spec.loader is not None
        self.lib = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.lib)
        self.lib.get_hermes_home = lambda: self.home

    def make(self, rel):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('durable content')
        return p

    def test_auto_classification(self):
        for name in ['.worktrees', 'worktrees']:
            for prefix in ['', 'repo/', 'arbitrary/nested/']:
                with self.subTest(name=name, prefix=prefix):
                    p = self.make(prefix + name + '/feature/test_durable.py')
                    self.assertIsNone(self.lib.guess_category(p))

    def test_stale_entries_and_deep_cleanup(self):
        for name in ['.worktrees', 'worktrees']:
            for category in ['test', 'temp', 'chrome-profile']:
                with self.subTest(name=name, category=category):
                    p = self.make('repo/' + name + '/feature/test_durable.py')
                    self.lib.save_tracked([entry(p, category)])
                    auto, prompt = self.lib.dry_run()
                    self.assertEqual((auto, prompt), ([], []))
                    self.lib.deep(confirm=lambda _: True)
                    self.assertEqual(p.read_text(), 'durable content')

    def test_wildcard_above_and_inside_worktree(self):
        for root_inside in [False, True]:
            with self.subTest(root_inside=root_inside):
                p = self.make('repo/.worktrees/feature/test_durable.py')
                junk = self.make('repo/test_disposable.py')
                root = p.parent if root_inside else self.home / 'repo'
                self.lib.save_tracked([entry(str(root) + '/*')])
                auto, _ = self.lib.dry_run()
                self.assertNotIn(str(p), {x['path'] for x in auto})
                self.lib.quick()
                self.assertTrue(p.exists())
                self.assertEqual(junk.exists(), root_inside)

    def test_recursive_parent_deletion(self):
        for category in ['temp', 'chrome-profile']:
            with self.subTest(category=category):
                p = self.make('repo/.worktrees/feature/test_durable.py')
                self.lib.save_tracked([entry(self.home / 'repo', category)])
                self.lib.deep(confirm=lambda _: True)
                self.assertEqual(p.read_text(), 'durable content')

    def test_empty_folders_and_unprotected_tests_still_removed(self):
        p = self.make('repo/.worktrees/feature/test_durable.py')
        empty = p.parent / 'empty'
        empty.mkdir()
        junk = self.make('test_disposable.py')
        self.lib.save_tracked([entry(junk)])
        self.lib.quick()
        self.assertTrue(p.exists())
        self.assertFalse(empty.exists())
        self.assertFalse(junk.exists())

    def test_substring_names_not_excluded(self):
        for name in ['worktrees-old', 'my.worktrees']:
            with self.subTest(name=name):
                p = self.make(name + '/test_disposable.py')
                self.assertEqual(self.lib.guess_category(p), 'test')
                self.lib.save_tracked([entry(p)])
                self.lib.quick()
                self.assertFalse(p.exists())

    def test_manual_tracking_rejected(self):
        p = self.make('repo/worktrees/feature/durable.txt')
        self.assertFalse(self.lib.track(str(p), 'temp', silent=True))

    def test_session_end_hook_preserves_worktree_and_cleans_junk(self):
        import sys
        source = Path(__file__).resolve().parents[2] / 'plugins/disk-cleanup/__init__.py'
        name = 'worktree_exclusion_hook_case'
        spec = importlib.util.spec_from_file_location(
            name, source, submodule_search_locations=[str(source.parent)])
        assert spec is not None and spec.loader is not None
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[name] = plugin
        self.addCleanup(sys.modules.pop, name, None)
        self.addCleanup(sys.modules.pop, name + '.disk_cleanup', None)
        spec.loader.exec_module(plugin)
        plugin.dg.get_hermes_home = lambda: self.home
        durable = self.make('repo/worktrees/feature/test_durable.py')
        junk = self.make('test_disposable.py')
        for p in [durable, junk]:
            plugin._on_post_tool_call(tool_name='write_file',
                                     args={'path': str(p)}, session_id='fixture')
        plugin._on_session_end(session_id='fixture')
        self.assertTrue(durable.exists())
        self.assertFalse(junk.exists())

    def test_stale_wildcard_via_worktree_alias_preserved(self):
        p = self.make('ordinary/feature/durable.txt')
        repo = self.home / 'repo'
        repo.mkdir()
        alias = repo / 'worktrees'
        alias.symlink_to(self.home / 'ordinary', target_is_directory=True)
        self.lib.save_tracked([entry(str(alias / 'feature') + '/*')])
        auto, _ = self.lib.dry_run()
        self.assertEqual(auto, [])
        self.lib.quick()
        self.assertTrue(p.exists())

    def test_symlink_to_worktree_preserved(self):
        p = self.make('repo/worktrees/feature/test_durable.py')
        alias = self.home / 'test_alias.py'
        alias.symlink_to(p)
        self.lib.save_tracked([entry(alias)])
        self.lib.quick()
        self.assertTrue(p.exists() and alias.exists())


if __name__ == '__main__':
    unittest.main()
