import importlib.util
import shutil
import sys
from pathlib import Path

import yaml


def test_private_journal_plugin_discovery_registers_command_and_aux_task(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["private-journal"]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    manager = PluginManager()
    manager.discover_and_load()

    command = manager._plugin_commands["log"]
    assert command["plugin"] == "private-journal"
    assert command["verbatim_args"] is True
    assert command["inline_while_busy"] is True
    assert "private_journal_batch" in manager._aux_tasks


def test_private_journal_wrapper_imports_from_profile_scripts_location(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[3]
    home = tmp_path / ".hermes"
    scripts = home / "scripts"
    scripts.mkdir(parents=True)
    wrapper = scripts / "process_private_journal.py"
    shutil.copy2(
        repo_root / "plugins" / "private_journal" / "scripts" / "process_private_journal.py",
        wrapper,
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.syspath_prepend(str(repo_root))

    spec = importlib.util.spec_from_file_location("profile_process_private_journal", wrapper)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert callable(module.main)
