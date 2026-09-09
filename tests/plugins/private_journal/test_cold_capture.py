"""First-import capture must not initialize general Hermes configuration."""
import os
import subprocess
import sys
from pathlib import Path


def test_cold_capture_rejects_symlink_without_config_side_effects(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    probe = tmp_path / 'capture_probe.py'
    probe.write_text(
        'import sys\n'
        'from plugins.private_journal import capture\n'
        'assert "hermes_cli.config" not in sys.modules\n'
        'try:\n'
        '    capture.capture_log("fictional cold-import fixture")\n'
        'except capture.PrivateJournalStoreError:\n'
        '    pass\n'
        'else:\n'
        '    raise AssertionError("unsafe capture unexpectedly succeeded")\n'
        'assert "hermes_cli.config" not in sys.modules\n',
        encoding='utf-8',
    )
    source = Path(__file__).resolve().parents[3]
    env = {**os.environ, 'HERMES_HOME': str(link / 'home'),
           'PYTHONPATH': str(source) + os.pathsep + os.environ.get('PYTHONPATH', '')}
    result = subprocess.run([sys.executable, str(probe)], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert list(real.iterdir()) == []
