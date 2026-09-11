"""Ancillary write validation through the real cron persistence boundary."""
from pathlib import Path
import tempfile
import unittest
from cron import jobs

class AncillaryValidationTests(unittest.TestCase):
    def test_create_update_validate_without_rewriting_legacy_reads(self):
        with tempfile.TemporaryDirectory() as tmp, jobs.use_cron_store(tmp):
            for field, values in {'name': [[], {}, 12, False], 'enabled_toolsets': ['terminal', [1], [{}], ['bad name'], ['../bad name']]}.items():
                for value in values:
                    with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                        jobs.create_job('Summarize fixture.', 'every 1h', **{field: value})
            self.assertFalse((Path(tmp) / 'cron/jobs.json').exists())
            row = jobs.create_job('Summarize fixture.', 'every 1h', name=' fixture ', enabled_toolsets=[' terminal ', 'terminal', 'file', ''])
            self.assertEqual(row['name'], 'fixture')
            self.assertEqual(row['enabled_toolsets'], ['terminal', 'file'])
            path = Path(tmp) / 'cron/jobs.json'; before = path.read_bytes()
            for update in ({'name': {}}, {'enabled_toolsets': [False]}, {'enabled_toolsets': 'file'}):
                with self.assertRaises(ValueError): jobs.update_job(row['id'], update)
                self.assertEqual(path.read_bytes(), before)
            updated = jobs.update_job(row['id'], {'name': ' next ', 'enabled_toolsets': []})
            self.assertEqual(updated['name'], 'next'); self.assertIsNone(updated['enabled_toolsets'])
            # Readers do not impose new validation on untouched legacy records.
            legacy = jobs.load_jobs(); legacy[0]['enabled_toolsets'] = ['historical odd value']
            jobs.save_jobs(legacy)
            jobs.update_job(row['id'], {'name': 'renamed'})
            self.assertEqual(jobs.load_jobs()[0]['enabled_toolsets'], ['historical odd value'])
