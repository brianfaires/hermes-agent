"""Exact Node pins must follow Termux revisions without changing upstream version."""

from dataclasses import replace

import pytest

from pm import termux_libs
from pm.package import InstallError
from pm.packages import Nodejs


def pool_row(version="26.13.1"):
    return termux_libs.PoolPackage(
        "nodejs", version,
        f"{termux_libs.POOL}pool/main/n/nodejs/nodejs_{version}_aarch64.deb",
        "a" * 64,
    )


@pytest.mark.parametrize("revision", ["", "-1", "-12"])
def test_exact_termux_version_and_provenance(monkeypatch, revision):
    row = pool_row("26.13.1" + revision)
    monkeypatch.setattr(termux_libs, "index", lambda: {"nodejs": row})
    node = Nodejs()
    assert node.fetch_url("26.13.1", "linux-arm64-bionic") == row.url
    assert node.known_sha256("26.13.1", row.url) == row.sha256
    assert node.known_sha256("26.13.1", node.fetch_url("26.13.1", "linux-x64")) is None


@pytest.mark.parametrize("row", [
    None,
    pool_row("26.12.1"),
    pool_row("26.13.10"),
    pool_row("26.13.1-rc.1"),
    replace(pool_row(), name="nodejs-lts"),
    replace(pool_row(), url="https://example.org/nodejs_26.13.1_aarch64.deb"),
    replace(pool_row(), url=pool_row().url.replace("/n/nodejs/", "/n/nodejs/../")),
    replace(pool_row(), url=pool_row().url.replace("aarch64", "arm")),
    replace(pool_row(), url=pool_row().url + "?mirror=1"),
    replace(pool_row(), sha256="not-a-digest"),
])
def test_termux_rejects_missing_mismatched_or_untrusted_rows(monkeypatch, row):
    monkeypatch.setattr(termux_libs, "index", lambda: {"nodejs": row} if row else {})
    with pytest.raises(InstallError):
        Nodejs().fetch_url("26.13.1", "linux-arm64-bionic")
