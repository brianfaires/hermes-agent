"""Real API schema/storage roundtrips for execution-local restrictions."""
from pathlib import Path

import pytest
from pydantic import ValidationError


MODEL = "gpt-6.1-sol"
PROVIDER = "openai-codex"


@pytest.fixture
def policy_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    return home


@pytest.mark.parametrize("restricted", [False, True])
def test_cron_dashboard_policy_roundtrip(policy_home, restricted):
    from cron.jobs import get_job, use_cron_store
    from hermes_cli import web_server

    job = web_server._create_cron_job_sync(web_server.CronJobCreate(
        prompt="API roundtrip", schedule="30m", subscription_only=restricted,
        provider=PROVIDER, model=MODEL,
    ), profile="default")
    with use_cron_store(policy_home):
        stored = get_job(job["id"])
    assert stored["subscription_only"] is restricted
    assert (stored["provider"], stored["model"]) == (PROVIDER, MODEL)
    if restricted:
        with pytest.raises(web_server.HTTPException) as error:
            web_server._update_cron_job_sync(job["id"], web_server.CronJobUpdate(
                updates={"provider": "openrouter"}), profile="default")
        assert error.value.status_code == 400
        with use_cron_store(policy_home):
            assert get_job(job["id"])["provider"] == PROVIDER


@pytest.mark.parametrize("restricted", [False, True])
def test_kanban_dashboard_policy_roundtrip(policy_home, restricted):
    from hermes_cli import kanban_db
    from plugins.kanban.dashboard import plugin_api

    result = plugin_api.create_task(plugin_api.CreateTaskBody(
        title="API roundtrip", subscription_only=restricted,
        provider_override=PROVIDER, model_override=MODEL,
    ), board="default")
    assert result["task"]["subscription_only"] is restricted
    with kanban_db.connect_closing() as conn:
        stored = kanban_db.get_task(conn, result["task"]["id"])
    assert stored.subscription_only is restricted
    assert (stored.provider_override, stored.model_override) == (PROVIDER, MODEL)


@pytest.mark.parametrize("value", ["false", 1, None])
def test_dashboard_policy_requires_boolean(value):
    from hermes_cli.web_models import CronJobCreate
    from plugins.kanban.dashboard.plugin_api import CreateTaskBody

    with pytest.raises(ValidationError):
        CronJobCreate(schedule="30m", subscription_only=value)
    with pytest.raises(ValidationError):
        CreateTaskBody(title="invalid", subscription_only=value)
