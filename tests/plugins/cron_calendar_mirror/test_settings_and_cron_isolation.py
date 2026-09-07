"""Settings resolution, plugin surface, and non-interference with cron.

The last group is the point of the whole design: this plugin reads cron state
and never writes it, so a broken or unconfigured mirror cannot degrade the
scheduler. Those tests drive the real ``cron.jobs`` and ``cron.executions``
stores in a temporary HERMES_HOME and assert byte-level immutability.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from plugins.cron_calendar_mirror import mirror
from plugins.cron_calendar_mirror.mirror import ConfigurationError, Settings, load_settings


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "cron").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def write_config(home: Path, settings: dict) -> None:
    payload = {"plugins": {"entries": {"cron_calendar_mirror": {"settings": settings}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(payload))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def test_settings_come_from_the_plugin_namespace(config_home):
    write_config(config_home, {
        "calendar_id": "hermes-crons@group.calendar.google.com",
        "horizon_days": 14,
        "high_frequency_minutes": 30,
        "include_error_detail": False,
    })

    settings = load_settings()

    assert settings.calendar_id == "hermes-crons@group.calendar.google.com"
    assert settings.horizon_days == 14
    assert settings.high_frequency_minutes == 30
    assert settings.include_error_detail is False


def test_missing_calendar_id_is_a_configuration_error(config_home):
    write_config(config_home, {"horizon_days": 5})

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings()

    assert "calendar_id" in str(excinfo.value)


def test_no_config_at_all_is_a_configuration_error(config_home):
    with pytest.raises(ConfigurationError):
        load_settings()


def test_primary_is_refused_at_configuration_time(config_home):
    write_config(config_home, {"calendar_id": "primary"})

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings()

    assert "primary" in str(excinfo.value)


@pytest.mark.parametrize(
    "given, expected",
    [
        ({"horizon_days": 0}, 1),
        ({"horizon_days": 900}, 31),
        ({"horizon_days": "nonsense"}, 7),
        ({"horizon_days": None}, 7),
    ],
)
def test_horizon_is_clamped_to_a_sane_range(config_home, given, expected):
    write_config(config_home, {"calendar_id": "c@group.calendar.google.com", **given})

    assert load_settings().horizon_days == expected


def test_cli_overrides_beat_config_and_none_is_ignored(config_home):
    write_config(config_home, {
        "calendar_id": "configured@group.calendar.google.com",
        "horizon_days": 7,
    })

    settings = load_settings({
        "calendar_id": "override@group.calendar.google.com",
        "horizon_days": None,
    })

    assert settings.calendar_id == "override@group.calendar.google.com"
    assert settings.horizon_days == 7


def test_result_page_size_is_bounded(config_home):
    write_config(config_home, {
        "calendar_id": "c@group.calendar.google.com", "result_page_size": 100000,
    })

    assert load_settings().result_page_size == 500


# ---------------------------------------------------------------------------
# Plugin surface
# ---------------------------------------------------------------------------

class _RecordingCtx:
    def __init__(self):
        self.commands = []
        self.hooks = []

    def register_command(self, name, handler=None, description=""):
        self.commands.append((name, handler, description))

    def register_hook(self, name, handler):  # pragma: no cover - must never run
        self.hooks.append((name, handler))


def test_register_adds_one_command_and_no_hooks():
    from plugins import cron_calendar_mirror

    ctx = _RecordingCtx()
    cron_calendar_mirror.register(ctx)

    assert [name for name, _, _ in ctx.commands] == ["cron-calendar-mirror"]
    assert ctx.hooks == [], "the mirror must not subscribe to any lifecycle hook"


def test_slash_status_works_without_configuration(config_home):
    from plugins import cron_calendar_mirror

    output = cron_calendar_mirror._handle_slash("status")

    assert "NOT CONFIGURED" in output
    assert "prerequisites" in output


def test_slash_sync_without_configuration_reports_instead_of_raising(config_home):
    from plugins import cron_calendar_mirror

    output = cron_calendar_mirror._handle_slash("sync")

    assert output.startswith("cron_calendar_mirror: not ready")


def test_slash_unknown_subcommand_returns_help(config_home):
    from plugins import cron_calendar_mirror

    assert "Unknown subcommand: wat" in cron_calendar_mirror._handle_slash("wat")
    assert "Subcommands:" in cron_calendar_mirror._handle_slash("help")


def test_cli_status_exits_zero(config_home, capsys):
    assert mirror.main(["status"]) == 0
    assert "cron_calendar_mirror" in capsys.readouterr().out


def test_cli_sync_without_configuration_exits_two(config_home, capsys):
    assert mirror.main(["sync"]) == 2
    assert "not ready" in capsys.readouterr().err


def test_cli_sync_without_credentials_exits_two(config_home, capsys):
    write_config(config_home, {"calendar_id": "c@group.calendar.google.com"})

    assert mirror.main(["sync"]) == 2
    assert "not ready" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Cron is read, never written
# ---------------------------------------------------------------------------

@pytest.fixture
def cron_home(tmp_path, monkeypatch):
    """A real cron store plus a real execution ledger in a temp profile."""
    from cron import executions as executions_module
    from cron.jobs import use_cron_store

    home = tmp_path / "cron-home"
    (home / "cron").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(executions_module, "EXECUTIONS_FILE", home / "cron" / "executions.db")
    with use_cron_store(home):
        yield home


def _digest(home: Path) -> dict:
    """Byte snapshot of the cron *job* store.

    ``cron/executions.db`` is deliberately excluded: opening the ledger
    read-only still creates the SQLite file when it does not exist yet, which
    is a read artefact rather than a state change. Ledger rows are asserted
    separately.
    """
    out = {}
    for path in sorted((home / "cron").rglob("*")):
        if path.is_file() and "executions.db" not in path.name:
            out[str(path.relative_to(home))] = path.read_bytes()
    return out


def test_collect_state_reads_real_cron_state_without_mutating_it(cron_home):
    from cron.executions import create_execution, finish_execution, list_executions
    from cron.jobs import create_job

    job = create_job(prompt="say hello", schedule="every day at 9am", name="Morning")
    execution = create_execution(job["id"], source="scheduler")
    finish_execution(execution["id"], success=True)

    before = _digest(cron_home)
    ledger_before = list_executions(limit=500)
    jobs, executions = mirror.collect_state(
        Settings(calendar_id="c@group.calendar.google.com")
    )
    after = _digest(cron_home)

    assert [item["id"] for item in jobs] == [job["id"]]
    assert [item["id"] for item in executions] == [execution["id"]]
    assert executions[0]["status"] == "completed"
    assert before == after, "reconciliation must never write to the cron store"
    assert list_executions(limit=500) == ledger_before


def test_a_full_sync_attempt_leaves_cron_untouched_when_credentials_are_missing(
    cron_home, capsys
):
    from cron.jobs import create_job

    from cron.executions import list_executions

    create_job(prompt="say hello", schedule="30m", name="Frequent")
    write_config(cron_home, {"calendar_id": "c@group.calendar.google.com"})
    before = _digest(cron_home)

    assert mirror.main(["sync"]) == 2

    assert _digest(cron_home) == before
    assert list_executions(limit=500) == []
    assert "not ready" in capsys.readouterr().err


def test_real_jobs_and_executions_project_into_a_coherent_desired_state(cron_home):
    from cron.executions import create_execution, finish_execution
    from cron.jobs import create_job

    daily = create_job(prompt="digest", schedule="every day at 9am", name="Daily digest")
    once = create_job(prompt="one shot", schedule="in 2h", name="One shot")
    failed = create_execution(daily["id"], source="scheduler")
    finish_execution(failed["id"], success=False, error="exit code 1")

    settings = Settings(calendar_id="c@group.calendar.google.com", horizon_days=2)
    jobs, executions = mirror.collect_state(settings)
    now = mirror.hermes_now()

    schedule_events = mirror.build_schedule_events(
        jobs, profile="test", settings=settings, now=now, tz_name=None
    )
    run_events = mirror.build_run_events(
        executions,
        jobs_by_id={item["id"]: item for item in jobs},
        profile="test",
        settings=settings,
        now=now,
        tz_name=None,
    )

    assert {event.private_props[mirror.PROP_JOB] for event in schedule_events} == {
        daily["id"], once["id"]
    }
    assert len(run_events) == 1
    assert run_events[0].private_props[mirror.PROP_EXECUTION] == failed["id"]
    assert run_events[0].color_id == mirror.COLOR_FAILED
    assert "exit code 1" in run_events[0].description
    # Identity is stable across a second projection of identical state.
    assert [event.id for event in schedule_events] == [
        event.id
        for event in mirror.build_schedule_events(
            jobs, profile="test", settings=settings, now=now, tz_name=None
        )
    ]


def test_executions_survive_the_job_being_deleted(cron_home):
    """The ledger, not the latest-output file, is the source for removed jobs."""
    from cron.executions import create_execution, finish_execution, list_executions
    from cron.jobs import create_job, remove_job

    job = create_job(prompt="digest", schedule="every day at 9am", name="Doomed")
    execution = create_execution(job["id"], source="scheduler")
    finish_execution(execution["id"], success=True)
    assert remove_job(job["id"]) is True

    settings = Settings(calendar_id="c@group.calendar.google.com")
    jobs, executions = mirror.collect_state(settings)
    assert jobs == []
    assert [item["id"] for item in executions] == [execution["id"]]

    events = mirror.build_run_events(
        executions, jobs_by_id={}, profile="test", settings=settings,
        now=mirror.hermes_now(), tz_name=None,
    )

    assert len(events) == 1
    assert "(removed)" in events[0].summary
    assert events[0].private_props[mirror.PROP_JOB] == job["id"]
