from __future__ import annotations

import sys
from dataclasses import replace

import pytest

from plugins.platforms.discord.kanban_mirror import __main__ as mirror_cli
from plugins.platforms.discord.kanban_mirror.config import MirrorConfig


def test_live_once_rejects_board_override_without_owner_profile_config(monkeypatch):
    cfg = MirrorConfig(enabled=False, board="default", forum_channel_id="")
    monkeypatch.setattr(mirror_cli, "load_mirror_config", lambda: cfg)
    monkeypatch.setattr(sys, "argv", ["kanban-mirror", "--once", "--board", "operations"])
    monkeypatch.setattr(
        mirror_cli,
        "connect_mirror",
        lambda *_args, **_kwargs: pytest.fail("invalid live config must not open the mirror database"),
    )

    with pytest.raises(SystemExit, match="active Hermes profile"):
        mirror_cli.main()


def test_live_once_accepts_complete_owner_profile_config(monkeypatch, tmp_path):
    cfg = MirrorConfig(
        enabled=True,
        board="operations",
        forum_channel_id="forum",
        token_env_path=tmp_path / ".env",
    )
    monkeypatch.setattr(mirror_cli, "load_mirror_config", lambda: cfg)
    monkeypatch.setattr(sys, "argv", ["kanban-mirror", "--once"])
    monkeypatch.setattr(mirror_cli, "load_discord_token", lambda _path: "token")
    monkeypatch.setattr(mirror_cli, "DiscordClient", lambda _token: object())
    monkeypatch.setattr(mirror_cli, "mirror_db_path", lambda _board: tmp_path / "mirror.db")
    monkeypatch.setattr(mirror_cli, "connect_mirror", lambda _path: object())

    async def fake_tick(_cfg, _client, _conn, **_kwargs):
        assert _cfg == replace(cfg)
        return ["ok"]

    monkeypatch.setattr(mirror_cli, "tick", fake_tick)
    mirror_cli.main()


def test_live_once_rejects_board_override_that_differs_from_owner_config(monkeypatch, tmp_path):
    cfg = MirrorConfig(
        enabled=True,
        board="default",
        forum_channel_id="default-forum",
        token_env_path=tmp_path / ".env",
    )
    monkeypatch.setattr(mirror_cli, "load_mirror_config", lambda: cfg)
    monkeypatch.setattr(sys, "argv", ["kanban-mirror", "--once", "--board", "operations"])
    monkeypatch.setattr(
        mirror_cli,
        "connect_mirror",
        lambda *_args, **_kwargs: pytest.fail("cross-board live execution must fail before opening state"),
    )

    with pytest.raises(SystemExit, match="does not match"):
        mirror_cli.main()