"""Upgrade fixtures preserve user files without cloning process control endpoints (#217)."""

from pathlib import Path
import socket
import tempfile

import pytest

from tests.e2e.core.upgrade.test_upgrade_path import _copy_user_state


@pytest.mark.platforms("posix")
def test_user_state_snapshot_omits_only_socket_entries():
    # Bind at a short path: the runner's private pytest paths can exceed AF_UNIX's limit.
    with tempfile.TemporaryDirectory(prefix="hermes-seed-", dir="/tmp") as directory:
        root = Path(directory)
        source = root / "home"
        profile = source / "profiles" / "work"
        profile.mkdir(parents=True)
        (source / "config.yaml").write_bytes(b"# keep comments\nmodel: custom\n")
        (profile / "state.db").write_bytes(b"owned database fixture")
        (source / "saved.sock").write_bytes(b"ordinary user file")
        (source / "config-link").symlink_to("config.yaml")
        (source / "endpoint-link").symlink_to("gateway.sock")
        with socket.socket(socket.AF_UNIX) as default, socket.socket(socket.AF_UNIX) as work:
            default.bind(str(root / "default"))
            work.bind(str(root / "work"))
            (root / "default").rename(source / "gateway.sock")
            (root / "work").rename(profile / "gateway.sock")
            destination = root / "snapshot"
            _copy_user_state(source, destination)

        assert not (destination / "gateway.sock").exists()
        assert not (destination / "profiles/work/gateway.sock").exists()
        for relative in ("config.yaml", "profiles/work/state.db", "saved.sock"):
            assert (destination / relative).read_bytes() == (source / relative).read_bytes()
        assert (destination / "config-link").is_symlink()
        assert (destination / "config-link").readlink() == Path("config.yaml")
        assert (destination / "endpoint-link").is_symlink()
        assert (destination / "endpoint-link").readlink() == Path("gateway.sock")


def test_user_state_snapshot_keeps_regular_file_named_gateway_socket(tmp_path):
    source = tmp_path / "home"
    source.mkdir()
    (source / "gateway.sock").write_bytes(b"user data, not a control socket")
    destination = tmp_path / "snapshot"
    _copy_user_state(source, destination)
    assert (destination / "gateway.sock").read_bytes() == (source / "gateway.sock").read_bytes()
