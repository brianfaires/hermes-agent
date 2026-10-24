"""Backup reporting follows the archived installation, including named profiles."""
from argparse import Namespace
from pathlib import Path
import zipfile

from hermes_cli import backup


def test_named_profile_backup_banner_names_archived_root(tmp_path, monkeypatch, capsys):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "writer"
    profile.mkdir(parents=True)
    (root / "config.yaml").write_text("model: default\n")
    (profile / "config.yaml").write_text("model: writer\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    output = tmp_path / "backup.zip"
    assert backup.run_backup(Namespace(output=str(output)))
    with zipfile.ZipFile(output) as archive:
        assert archive.read("config.yaml") == b"model: default\n"
        assert archive.read("profiles/writer/config.yaml") == b"model: writer\n"
    assert "Scanning ~/.hermes ..." in capsys.readouterr().out
