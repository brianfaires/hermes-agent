"""Tests for systemd optional-directive normalization (issue #41119).

On older systemd versions that don't support RestartMaxDelaySec /
RestartSteps, the installed unit file has those directives silently
dropped.  Without normalization, systemd_unit_is_current() would
perpetually report the unit as outdated because the strict text
comparison sees a difference.

The fix: _strip_optional_systemd_directives() removes those directives
from both the installed and expected text before comparison.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# _strip_optional_systemd_directives
# ---------------------------------------------------------------------------


class TestStripOptionalSystemdDirectives:
    def test_removes_restart_max_delay_sec(self):
        from hermes_cli.gateway import _strip_optional_systemd_directives
        text = """[Service]
Restart=always
RestartSec=5
RestartMaxDelaySec=300
RestartSteps=5
"""
        result = _strip_optional_systemd_directives(text)
        assert "RestartMaxDelaySec" not in result
        assert "RestartSteps" not in result
        assert "Restart=always" in result
        assert "RestartSec=5" in result





    def test_full_unit_comparison(self):
        """Simulate the full stale-check flow with an older systemd unit."""
        from hermes_cli.gateway import (
            _normalize_service_definition,
            _strip_optional_systemd_directives,
        )
        # What the installed unit looks like on older systemd (directives stripped)
        installed = """[Unit]
Description=Hermes Gateway
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python -m hermes_cli.main gateway run
Restart=always
RestartSec=5
KillMode=mixed
KillSignal=SIGTERM

[Install]
WantedBy=default.target
"""
        # What generate_systemd_unit produces (with the directives)
        expected = """[Unit]
Description=Hermes Gateway
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python -m hermes_cli.main gateway run
Restart=always
RestartSec=5
RestartMaxDelaySec=300
RestartSteps=5
KillMode=mixed
KillSignal=SIGTERM

[Install]
WantedBy=default.target
"""
        # Without normalization, they differ
        assert _normalize_service_definition(installed) != _normalize_service_definition(expected)

        # With optional-directive stripping, they match
        norm_installed = _normalize_service_definition(
            _strip_optional_systemd_directives(installed)
        )
        norm_expected = _normalize_service_definition(
            _strip_optional_systemd_directives(expected)
        )
        assert norm_installed == norm_expected


# ---------------------------------------------------------------------------
# systemd_unit_is_current integration
# ---------------------------------------------------------------------------


class TestSystemdUnitIsCurrent:
    def test_unit_without_fatal_config_restart_policy_is_not_current(
        self, tmp_path, monkeypatch,
    ):
        from hermes_cli import gateway as gw

        expected = """[Service]
Restart=always
RestartForceExitStatus=75
RestartPreventExitStatus=78
"""
        installed = expected.replace("RestartPreventExitStatus=78\n", "")
        unit_file = tmp_path / "hermes-gateway.service"
        unit_file.write_text(installed)

        monkeypatch.setattr(gw, "get_systemd_unit_path", lambda system=False: unit_file)
        monkeypatch.setattr(
            gw,
            "generate_systemd_unit",
            lambda system=False, run_as_user=None: expected,
        )

        assert gw.systemd_unit_is_current(system=False) is False

    def test_unit_without_optional_directives_is_current(self, tmp_path, monkeypatch):
        """Installed unit missing RestartMaxDelaySec/RestartSteps should be
        considered current when the generated unit includes them."""
        from hermes_cli import gateway as gw

        installed = """[Unit]
Description=Hermes Gateway

[Service]
Type=simple
ExecStart=/usr/bin/python gateway run
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""
        unit_file = tmp_path / "hermes-gateway.service"
        unit_file.write_text(installed)

        monkeypatch.setattr(gw, "get_systemd_unit_path", lambda system=False: unit_file)
        monkeypatch.setattr(
            gw,
            "generate_systemd_unit",
            lambda system=False, run_as_user=None: installed + "\nRestartMaxDelaySec=300\nRestartSteps=5\n",
        )

        assert gw.systemd_unit_is_current(system=False) is True


    def test_nonexistent_unit_is_not_current(self, tmp_path, monkeypatch):
        from hermes_cli import gateway as gw
        unit_file = tmp_path / "nonexistent.service"
        monkeypatch.setattr(gw, "get_systemd_unit_path", lambda system=False: unit_file)
        assert gw.systemd_unit_is_current(system=False) is False


# ---------------------------------------------------------------------------
# Drop-in overrides (<unit>.d/*.conf)
# ---------------------------------------------------------------------------

_GENERATED = """[Unit]
Description=Hermes Gateway
After=network-online.target

[Service]
Type=simple
ExecStart=/opt/new/venv/bin/python -m hermes_cli.main gateway run
WorkingDirectory=/home/u/.hermes
Environment="PATH=/opt/new/venv/bin:/home/u/.nvm/versions/node/v24.18.1/bin:/usr/bin:/bin"
Environment="VIRTUAL_ENV=/opt/new/venv"
Environment="HERMES_HOME=/home/u/.hermes"
Restart=always
TimeoutStopSec=90

[Install]
WantedBy=default.target
"""

# Base unit as last written by an older install: node upgraded since, and the
# operator pins ExecStart/PATH/VIRTUAL_ENV in drop-ins.
_INSTALLED_STALE_SHADOWED = _GENERATED.replace("/opt/new/venv", "/opt/old/venv").replace(
    "v24.18.1", "v24.15.0"
)

_PINNING_DROPINS = {
    "10-runtime.conf": (
        "# operator pin\n[Service]\nExecStart=\n"
        "ExecStart=/opt/old/venv/bin/python -m hermes_cli.main gateway run\n"
        'Environment="VIRTUAL_ENV=/opt/old/venv"\n'
    ),
    "30-path.conf": (
        '[Service]\nEnvironment="PATH=/home/u/.local/bin:/opt/old/venv/bin:/usr/bin"\n'
    ),
    "hardening.conf": "[Service]\nNoNewPrivileges=yes\nMemoryMax=2G\n",
}


def _install(tmp_path, monkeypatch, base, dropins, generated=_GENERATED):
    from hermes_cli import gateway as gw

    unit_file = tmp_path / "hermes-gateway.service"
    unit_file.write_text(base)
    dropin_dir = tmp_path / "hermes-gateway.service.d"
    dropin_dir.mkdir()
    for name, body in dropins.items():
        (dropin_dir / name).write_text(body)

    monkeypatch.setattr(gw, "get_systemd_unit_path", lambda system=False: unit_file)
    monkeypatch.setattr(
        gw, "generate_systemd_unit", lambda system=False, run_as_user=None: generated
    )
    monkeypatch.setattr(gw, "_read_systemd_unit_environment", lambda system=False: {})
    systemctl_calls = []
    monkeypatch.setattr(
        gw, "_run_systemctl", lambda args, **kw: systemctl_calls.append(args)
    )
    return gw, unit_file, systemctl_calls


class TestSystemdDropinOverrides:
    @pytest.mark.parametrize("system", [False, True])
    def test_difference_only_in_dropin_overridden_values_is_current(
        self, tmp_path, monkeypatch, system
    ):
        gw, _, _ = _install(
            tmp_path, monkeypatch, _INSTALLED_STALE_SHADOWED, _PINNING_DROPINS
        )
        assert gw.systemd_unit_is_current(system=system) is True

    def test_same_unit_without_dropins_is_outdated(self, tmp_path, monkeypatch):
        gw, _, _ = _install(tmp_path, monkeypatch, _INSTALLED_STALE_SHADOWED, {})
        assert gw.systemd_unit_is_current(system=False) is False

    def test_dropin_does_not_mask_unrelated_stale_directive(self, tmp_path, monkeypatch):
        installed = _INSTALLED_STALE_SHADOWED.replace(
            "TimeoutStopSec=90", "TimeoutStopSec=60"
        )
        dropins = dict(_PINNING_DROPINS)
        dropins["20-cron.conf"] = '[Service]\nEnvironment="HERMES_CRON_TIMEOUT=1800"\n'
        gw, _, _ = _install(tmp_path, monkeypatch, installed, dropins)
        assert gw.systemd_unit_is_current(system=False) is False

    def test_override_in_other_section_does_not_mask(self, tmp_path, monkeypatch):
        installed = _GENERATED.replace("TimeoutStopSec=90", "TimeoutStopSec=60")
        gw, _, _ = _install(
            tmp_path, monkeypatch, installed, {"x.conf": "[Unit]\nTimeoutStopSec=60\n"}
        )
        assert gw.systemd_unit_is_current(system=False) is False

    def test_accumulating_directive_without_reset_does_not_mask(
        self, tmp_path, monkeypatch
    ):
        # A drop-in ExecStart= with no empty reset APPENDS; the base ExecStart
        # is still live, so its staleness still matters.
        installed = _GENERATED.replace(
            "/opt/new/venv/bin/python", "/opt/old/venv/bin/python"
        )
        gw, _, _ = _install(
            tmp_path,
            monkeypatch,
            installed,
            {"x.conf": "[Service]\nExecStart=/opt/old/venv/bin/python -m x\n"},
        )
        assert gw.systemd_unit_is_current(system=False) is False

    def test_non_conf_files_in_dropin_dir_are_ignored(self, tmp_path, monkeypatch):
        dropins = {name + ".bak": body for name, body in _PINNING_DROPINS.items()}
        gw, _, _ = _install(tmp_path, monkeypatch, _INSTALLED_STALE_SHADOWED, dropins)
        assert gw.systemd_unit_is_current(system=False) is False

    @pytest.mark.parametrize("system", [False, True])
    def test_refresh_leaves_base_unit_alone_when_only_shadowed_values_differ(
        self, tmp_path, monkeypatch, system
    ):
        gw, unit_file, systemctl_calls = _install(
            tmp_path, monkeypatch, _INSTALLED_STALE_SHADOWED, _PINNING_DROPINS
        )
        assert gw.refresh_systemd_unit_if_needed(system=system) is False
        assert unit_file.read_text() == _INSTALLED_STALE_SHADOWED
        assert systemctl_calls == []

    def test_refresh_still_repairs_live_stale_directive(self, tmp_path, monkeypatch):
        installed = _INSTALLED_STALE_SHADOWED.replace(
            "TimeoutStopSec=90", "TimeoutStopSec=60"
        )
        gw, unit_file, systemctl_calls = _install(
            tmp_path, monkeypatch, installed, _PINNING_DROPINS
        )
        assert gw.refresh_systemd_unit_if_needed(system=False) is True
        assert unit_file.read_text() == _GENERATED
        assert ["daemon-reload"] in systemctl_calls
        # Drop-ins are untouched by the repair.
        assert (tmp_path / "hermes-gateway.service.d" / "30-path.conf").exists()
