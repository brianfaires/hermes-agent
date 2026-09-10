"""Execute only install.sh's source-extracted setup_path, never the installer."""
from pathlib import Path
import subprocess
import tempfile
import unittest


# Hosted OS selection; local qualification intentionally needs only unittest.
try:
    import pytest
except ModuleNotFoundError:
    pass
else:
    pytestmark = pytest.mark.linux_only


class InstallLauncherGuard(unittest.TestCase):
    def test_worktree_returns_before_launcher_or_shell_changes(self):
        source = (Path(__file__).resolve().parents[2] / 'scripts/install.sh').read_text()
        # The continuation explicitly permits shell-function extraction. Execute
        # the complete function; do not assert on its source or run bootstrap.
        function = source.split('\nsetup_path() {\n', 1)[1].split('\n}\n', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / 'linked checkout'
            checkout.mkdir()
            (checkout / '.git').write_text('gitdir: /unused/test-only\n')
            (root / '.bashrc').write_text('shell sentinel\n')
            (root / 'hermes').write_text('launcher sentinel\n')
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            script = 'log_info() { :; }\nlog_warn() { :; }\nsetup_path() {\n' + function + '\n}\nsetup_path\n'
            result = subprocess.run(['/bin/bash', '-eu', '-c', script], cwd=root,
                env={'HOME': tmp, 'HERMES_HOME': str(root / '.hermes'),
                     'HERMES_BUNDLES_DIR': str(root / 'bundles'),
                     'INSTALL_DIR': str(checkout), 'PATH': '/usr/bin:/bin'},
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
