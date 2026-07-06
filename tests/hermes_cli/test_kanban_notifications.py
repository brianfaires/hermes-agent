from pathlib import Path

from hermes_cli.kanban_notifications import resolve_notify_target


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
