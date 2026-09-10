"""Failed per-file spawns must not leak runner-owned temporary roots."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_tests_parallel as runner


class RunnerCleanupTests(unittest.TestCase):
    def test_spawn_failure_removes_actual_temporary_root(self):
        with tempfile.TemporaryDirectory() as root:
            attempt = Path(root) / 'attempt'
            attempt.mkdir()
            with patch.object(runner.tempfile, 'mkdtemp', return_value=str(attempt)), \
                 patch.object(runner.subprocess, 'Popen', side_effect=OSError('spawn failed')):
                with self.assertRaisesRegex(OSError, 'spawn failed'):
                    runner._run_one_file_once(Path('test_example.py'), [], Path(root), 10)
            self.assertFalse(attempt.exists())


if __name__ == '__main__':
    unittest.main()
