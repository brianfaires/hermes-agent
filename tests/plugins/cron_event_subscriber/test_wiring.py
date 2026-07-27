from __future__ import annotations

import importlib

import yaml

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


def test_register_wires_existing_plugin_command_and_session_hook():
    plugin = importlib.import_module("hermes_plugins.cron_event_subscriber")
    manager = PluginManager()
    manifest = PluginManifest(name="cron-event-subscriber", key="cron-event-subscriber")

    plugin.register(PluginContext(manifest, manager))

    assert "cron-events" in manager._plugin_commands
    assert manager._hooks["on_session_start"]


def test_bundled_plugin_is_discovered_when_enabled(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["cron-event-subscriber"]}}),
        encoding="utf-8",
    )

    manager = PluginManager()
    manager.discover_and_load(force=True)

    loaded = manager._plugins["cron-event-subscriber"]
    assert loaded.enabled is True
    assert "cron-events" in manager._plugin_commands
    assert manager._hooks["on_session_start"]


def test_registered_command_drains_pending_events(tmp_path, monkeypatch):
    root = tmp_path / "events" / "cron"
    monkeypatch.setenv("HERMES_CRON_EVENTS_DIR", str(root))
    pending = root / "pending" / "writer"
    pending.mkdir(parents=True)
    pending.joinpath("wired.json").write_text(
        '{"schema_version":1,"event_id":"wired","event_type":"create",'
        '"emitted_at":"2026-07-21T12:00:00Z","source_profile":"writer",'
        '"job_id":"job-wired","job":{"id":"job-wired","name":"Wired"}}',
        encoding="utf-8",
    )

    plugin = importlib.import_module("hermes_plugins.cron_event_subscriber")
    manager = PluginManager()
    plugin.register(PluginContext(PluginManifest(name="cron-event-subscriber"), manager))
    output = manager._plugin_commands["cron-events"]["handler"]("")

    assert "wired" in output
    assert not pending.joinpath("wired.json").exists()
    assert root.joinpath("acknowledged", "wired.json").exists()


def test_non_mapping_plugin_settings_fail_closed(monkeypatch):
    import hermes_cli.config as config_module

    plugin = importlib.import_module("hermes_plugins.cron_event_subscriber")
    monkeypatch.setattr(
        config_module,
        "load_config_readonly",
        lambda: {
            "plugins": {
                "entries": {"cron-event-subscriber": "not-a-settings-mapping"}
            }
        },
    )

    assert plugin._settings() == {}


def test_invalid_numeric_settings_use_bounded_defaults(tmp_path, monkeypatch):
    plugin = importlib.import_module("hermes_plugins.cron_event_subscriber")
    import cron.event_bus as event_bus

    monkeypatch.setattr(event_bus, "event_root", lambda: tmp_path)
    monkeypatch.setattr(
        plugin,
        "_settings",
        lambda: {
            "claim_timeout_seconds": "not-a-number",
            "retention_days": True,
            "temporary_retention_seconds": -5,
        },
    )

    subscriber = plugin._subscriber()

    assert subscriber.claim_timeout_seconds == 300
    assert subscriber.retention_days == 30
    assert subscriber.temporary_retention_seconds == 0


def test_command_limit_is_bounded_for_malformed_or_extreme_values(monkeypatch):
    plugin = importlib.import_module("hermes_plugins.cron_event_subscriber")

    assert plugin._bounded_int(
        {"max_events_per_drain": "garbage"},
        "max_events_per_drain",
        100,
        minimum=1,
        maximum=10000,
    ) == 100
    assert plugin._bounded_int(
        {"max_events_per_drain": 999999},
        "max_events_per_drain",
        100,
        minimum=1,
        maximum=10000,
    ) == 10000
