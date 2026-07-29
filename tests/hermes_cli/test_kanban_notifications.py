from pathlib import Path

import pytest

from hermes_cli.kanban_notifications import (
    is_notify_target_allowed,
    notification_policy,
    resolve_notify_target,
)


def _write_profile_config(home: Path, chat_id: str, thread_id: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "gateway:\n"
        "  platforms:\n"
        "    telegram:\n"
        "      enabled: true\n"
        "      token: test-token\n"
        "      home_channel:\n"
        "        platform: telegram\n"
        f"        chat_id: {chat_id}\n"
        f"        thread_id: {thread_id}\n",
        encoding="utf-8",
    )


def test_telegram_home_policy_resolves_notifier_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", raising=False)
    _write_profile_config(tmp_path, "default-chat", "default-thread")
    _write_profile_config(tmp_path / "profiles" / "ops", "ops-chat", "ops-thread")

    target = resolve_notify_target(
        platform="discord",
        chat_id="discord-channel",
        notifier_profile="ops",
        cfg={"kanban": {"notification_policy": {"mode": "telegram_home_only"}}},
    )

    assert target is not None
    assert target.chat_id == "ops-chat"
    assert target.thread_id == "ops-thread"
    assert target.notifier_profile == "ops"


def test_unknown_policy_mode_fails_closed():
    cfg = {"kanban": {"notification_policy": {"mode": "telegram_home_onyl"}}}

    assert resolve_notify_target(
        platform="discord",
        chat_id="discord-channel",
        cfg=cfg,
    ) is None
    assert is_notify_target_allowed("discord", cfg=cfg) is False


@pytest.mark.parametrize("malformed", [["origin"], [], 0, False, ""])
def test_malformed_notification_policy_fails_closed(malformed):
    cfg = {"kanban": {"notification_policy": malformed}}

    assert notification_policy(cfg)["mode"] == "deny"
    assert resolve_notify_target(platform="discord", chat_id="123", cfg=cfg) is None


def test_malformed_allowed_platforms_grants_nothing():
    cfg = {
        "kanban": {
            "notification_policy": {
                "mode": "deny",
                "allowed_platforms": 123,
                "preserve_tui": False,
            }
        }
    }

    assert notification_policy(cfg)["allowed_platforms"] == []
    assert resolve_notify_target(platform="discord", chat_id="123", cfg=cfg) is None
