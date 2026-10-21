"""Only live differences may trigger a systemd unit repair."""
from pathlib import Path

import pytest

from hermes_cli import gateway as gw


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('system', [False, True])
def test_effective_dropins_preserve_base_and_live_drift(tmp_path, monkeypatch, system):
    unit = tmp_path / 'hermes-gateway.service'
    installed = ('[Unit]\nAfter=network.target\n[Service]\nExecStart=/old\n'
                 'WorkingDirectory=/old\nEnvironment="PATH=/old" "KEEP=original"\n'
                 'TimeoutStopSec=60\n')
    expected = installed.replace('/old', '/new')
    unit.write_text(installed)
    dropins = unit.with_name(unit.name + '.d')
    dropins.mkdir()
    (dropins / '10-runtime.conf').write_text(
        '[Service]\nExecStart=\nExecStart=/pinned\nWorkingDirectory=/pinned\n'
        'Environment="PATH=/pinned"\n')
    # Generic/vendor drop-ins need not live next to the installed unit.
    vendor = tmp_path / 'vendor space.conf'
    vendor.write_text('[Service]\nTimeoutStopSec=90\n')
    monkeypatch.setattr(gw, 'get_systemd_unit_path', lambda system=False: unit)
    monkeypatch.setattr(gw, '_sync_hermes_home_from_systemd_unit', lambda **kw: None)
    monkeypatch.setattr(gw, '_systemctl_show', lambda properties, **kw: {
        'DropInPaths': str(vendor).replace(' ', r'\x20')})
    monkeypatch.setattr(gw, 'generate_systemd_unit', lambda **kw: expected)
    monkeypatch.setattr(gw, '_run_systemctl', lambda *a, **kw: pytest.fail('unexpected repair'))
    assert gw.systemd_unit_is_current(system=system)
    assert not gw.refresh_systemd_unit_if_needed(system=system)
    assert unit.read_text() == installed
    expected = expected.replace('KEEP=original', 'KEEP=changed')
    assert not gw.systemd_unit_is_current(system=system)
    expected = installed.replace('After=network.target', 'After=other.target')
    (dropins / '20-extra.conf').write_text('[Unit]\nAfter=\nAfter=extra.target\n')
    assert not gw.systemd_unit_is_current(system=system)


@pytest.mark.platforms('linux')
def test_append_does_not_mask_base_but_reset_and_continuation_do(tmp_path, monkeypatch):
    unit = tmp_path / 'hermes-gateway.service'
    unit.write_text('[Service]\nExecStart=/old\nEnvironment="A=old" "B=old"\n')
    expected = '[Service]\nExecStart=/new\nEnvironment="A=new" "B=new"\n'
    folder = unit.with_name(unit.name + '.d')
    folder.mkdir()
    override = folder / 'override.conf'
    override.write_text('[Service]\nExecStart=/extra\nEnvironment="A=fixed"\n')
    monkeypatch.setattr(gw, 'get_systemd_unit_path', lambda system=False: unit)
    monkeypatch.setattr(gw, '_systemctl_show', lambda *a, **kw: {})
    monkeypatch.setattr(gw, 'generate_systemd_unit', lambda **kw: expected)
    assert not gw.systemd_unit_is_current()
    override.write_text('[Service]\nExecStart=\nExecStart=/pinned\n'
                        'Environment="A=fixed" \\\n# continuation comment\n "B=fixed"\n')
    assert gw.systemd_unit_is_current()
