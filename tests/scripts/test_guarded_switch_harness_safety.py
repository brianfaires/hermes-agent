"""Offline harness boundary checks; every subprocess call is denied."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


def load_helper():
    spec = importlib.util.spec_from_file_location(
        "guard_systemd_helper", Path(__file__).with_name("test_guarded_switch_systemd.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_import_collection_and_invocation_cannot_probe(tmp_path):
    with patch("subprocess.run", side_effect=AssertionError("manager forbidden")) as denied:
        helper = load_helper()
        assert helper.pytestmark.name == "skip"
        for fault in helper.FAULTS:
            with pytest.raises(pytest.skip.Exception):
                helper.test_real_systemd_guarded_switch(tmp_path, fault)
            with pytest.raises(SystemExit) as error:
                helper.main(["--case", fault])
            assert error.value.code == 2
        denied.assert_not_called()


def test_explicit_opt_in_dispatches_only_selected_case():
    with patch("subprocess.run", side_effect=AssertionError("manager forbidden")):
        helper = load_helper()
        selected = Mock()
        helper.main(["--allow-disposable-user-systemd", "--case", "command_timeout_parent"], selected)
        assert selected.call_count == 1
        assert selected.call_args.args[1] == "command_timeout_parent"


def test_manager_rejects_nonowned_units_and_global_actions_before_adapter():
    with patch("subprocess.run", side_effect=AssertionError("manager forbidden")):
        helper = load_helper()
        recorder = Mock(return_value=SimpleNamespace(stdout="MainPID=0\n"))
        prefix = "guarded-switch-test-" + "a" * 32
        manager = helper.OwnedManager(prefix, recorder)
        owned = prefix + "-app.service"
        for unit in ("hermes-gateway.service", prefix + "-other.service", owned + "x", "--all"):
            with pytest.raises(ValueError):
                manager.ctl("stop", unit)
            with pytest.raises(ValueError):
                manager.start(unit, [], ["/bin/true"])
        for verb in ("daemon-reload", "daemon-reexec", "kill", "list-units", "enable"):
            with pytest.raises(ValueError):
                manager.ctl(verb, owned)
        recorder.assert_not_called()
        assert manager.show(owned) == {"MainPID": "0"}
        manager.ctl("stop", owned)
        manager.start(owned, ["RemainAfterExit=yes"], ["/bin/true"])
        assert recorder.call_count == 3
