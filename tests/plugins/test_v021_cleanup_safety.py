"""Real filesystem regression checks; also collectable by hosted pytest."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone


class LegacyDirectorySafety(unittest.TestCase):
    def test_legacy_records_preserve_untracked_descendants(self):
        for category, relative in [('temp', 'scratch'), ('test', 'test_legacy'),
                                   ('cron-output', 'cron/output/legacy')]:
            with self.subTest(category=category), tempfile.TemporaryDirectory() as home:
                with patch.dict(os.environ, {'HERMES_HOME': home}):
                    path = Path(__file__).resolve().parents[2] / 'plugins/disk-cleanup/disk_cleanup.py'
                    spec = importlib.util.spec_from_file_location('cleanup_regression', path)
                    lib = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(lib)
                    directory = Path(home) / relative
                    directory.mkdir(parents=True)
                    child = directory / 'valuable.txt'
                    child.write_text('keep')
                    record = dict(path=str(directory), category=category, size=4,
                                  timestamp=(datetime.now(timezone.utc) - timedelta(days=60)).isoformat())
                    lib.save_tracked([record])
                    auto, _ = lib.dry_run()
                    self.assertNotIn(record, auto)
                    result = lib.quick()
                    self.assertEqual(child.read_text(), 'keep')
                    self.assertEqual(result['deleted'], 0)
                    self.assertIn(record, lib.load_tracked())

    def test_explicit_wildcard_preserves_fresh_and_symlink_targets(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as outside:
            with patch.dict(os.environ, {'HERMES_HOME': home}):
                path = Path(__file__).resolve().parents[2] / 'plugins/disk-cleanup/disk_cleanup.py'
                spec = importlib.util.spec_from_file_location('cleanup_regression', path)
                lib = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(lib)
                root = Path(home) / 'audio_cache'
                root.mkdir()
                old = root / 'old.wav'
                old.write_text('old')
                age = old.stat().st_mtime - 10 * 86400
                os.utime(old, (age, age))
                fresh = root / 'fresh.wav'
                fresh.write_text('new')
                valuable = Path(outside) / 'keep.txt'
                valuable.write_text('keep')
                (root / 'link').symlink_to(outside, target_is_directory=True)
                self.assertTrue(lib.track(f'{root}/*', 'temp', silent=True))
                auto, _ = lib.dry_run()
                self.assertEqual([item['path'] for item in auto], [str(old)])
                self.assertEqual(lib.quick()['deleted'], 1)
                self.assertFalse(old.exists())
                self.assertTrue(fresh.exists())
                self.assertEqual(valuable.read_text(), 'keep')
                self.assertFalse(lib.track(f'{home}/logs/*', 'temp', silent=True))


if __name__ == '__main__':
    unittest.main()
