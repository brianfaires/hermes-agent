"""A shared script/monitor gate remembers only admitted observations."""
import json

import pytest

from cron.jobs import create_job, get_job, use_cron_store
from cron.monitor import check_monitor


def test_shared_script_idle_does_not_consume_monitor_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = scripts / "gate.py"
    source.write_text('print(\'{"wakeAgent": false}\')\n', encoding="utf-8")
    with use_cron_store(tmp_path):
        job = create_job(prompt="Check scoped cards", schedule="every 5m",
                         script="gate.py", monitor_script="gate.py", deliver="local")
        idle = check_monitor(job)
        assert idle.ok and not idle.changed
        assert get_job(job["id"]).get("monitor_state") is None

        output = {"wakeAgent": True, "cards": [["t_12345678", "completed", 17]]}
        source.write_text(f"print({json.dumps(output)!r})\n", encoding="utf-8")
        admitted = check_monitor(get_job(job["id"]))
        assert admitted.ok and admitted.changed and admitted.first_run
        assert not check_monitor(get_job(job["id"])).changed

        # Busy/hold/expiry observations must not erase delivery deduplication.
        source.write_text('print(\'{"wakeAgent": false}\')\n', encoding="utf-8")
        assert not check_monitor(get_job(job["id"])).changed
        source.write_text(f"print({json.dumps(output)!r})\n", encoding="utf-8")
        assert not check_monitor(get_job(job["id"])).changed


@pytest.mark.parametrize("change", [{}, {"subscription_only": False},
    {"provider": "other"}, {"model": "other"}, {"attach_to_session": True},
    {"monitor_script": "other.py"}, {"base_url": "https://example.invalid"}])
def test_compact_wrapper_is_restricted_and_preserves_cron_guidance(tmp_path, monkeypatch, change):
    from cron.scheduler import _build_job_prompt
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with use_cron_store(tmp_path):
        job = create_job(prompt="Inspect admitted exception.", schedule="every 5m",
                         script="gate.py", monitor_script="gate.py", deliver="local",
                         subscription_only=True, provider="openai-codex", model="gpt-6.1-sol",
                         attach_to_session=False)
        job.update(change)
        prompt = _build_job_prompt(job, prerun_script=(True, '{"wakeAgent": true}'))
    assert "send_message" in prompt and "[SILENT]" in prompt
    assert "⚠️" in prompt and "❌" in prompt
    assert "Inspect admitted exception." in prompt
    if change:
        assert "[IMPORTANT: You are running as a scheduled cron job." in prompt
    else:
        assert len(prompt) < 600
        assert "Scheduled cron job, not the user" in prompt
