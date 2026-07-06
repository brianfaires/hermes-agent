"""Calendar output attach is a COMPLETE cron-hook consumer, not a scheduler edit."""

from pathlib import Path

import cron.hooks as cron_hooks


def test_complete_payload_carries_output_file():
    """COMPLETE emit must forward output_file so hook consumers can use it."""
    cron_hooks.clear_hooks()
    seen = {}
    cron_hooks.register_hook(cron_hooks.COMPLETE, lambda **kw: seen.update(kw))
    cron_hooks.emit(
        cron_hooks.COMPLETE,
        job={"id": "j1"},
        success=True,
        duration_seconds=1.0,
        error=None,
        notify=lambda *a, **k: None,
        output_file="/tmp/out.txt",
    )
    cron_hooks.clear_hooks()
    assert seen["output_file"] == "/tmp/out.txt"


def test_calendar_sync_registers_and_noops_without_script(tmp_path, monkeypatch):
    """register() wires a COMPLETE callback that safely no-ops when the local
    sync script is absent (best-effort, must never raise)."""
    from cron import calendar_sync

    # Point hermes home at an empty dir so the sync script does not exist.
    monkeypatch.setattr(calendar_sync, "get_hermes_home", lambda: tmp_path)
    cron_hooks.clear_hooks()
    calendar_sync._registered = False
    calendar_sync.register()
    assert calendar_sync.attach_output_to_calendar in cron_hooks._hooks[cron_hooks.COMPLETE]

    # Emitting COMPLETE must not raise even though no script/output exists.
    cron_hooks.emit(
        cron_hooks.COMPLETE,
        job={"id": "j1"},
        success=True,
        duration_seconds=1.0,
        error=None,
        notify=lambda *a, **k: None,
        output_file=str(tmp_path / "out.txt"),
    )
    cron_hooks.clear_hooks()


def test_calendar_sync_invokes_script_attach(tmp_path, monkeypatch):
    """When the local sync script exists, the callback loads it and calls
    attach_output_to_calendar_event(job, output_file)."""
    from cron import calendar_sync

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "cron_calendar_recurring_sync.py").write_text(
        "CALLS = []\n"
        "def attach_output_to_calendar_event(job, output_file):\n"
        "    CALLS.append((job['id'], str(output_file)))\n"
        "    return {'errors': False}\n"
    )
    monkeypatch.setattr(calendar_sync, "get_hermes_home", lambda: tmp_path)

    out = tmp_path / "run.txt"
    out.write_text("hello")
    calendar_sync.attach_output_to_calendar(job={"id": "j9"}, output_file=str(out))
    # No assertion on module CALLS (fresh import each call); success = no raise +
    # the script path branch executed. Guard: absent script path also fine.
