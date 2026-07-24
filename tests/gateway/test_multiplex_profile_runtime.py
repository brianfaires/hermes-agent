from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


def _runner(default_home: Path, *, secondaries: dict[str, dict] | None = None):
    return SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True),
        _gateway_profile_name="default",
        _gateway_profile_home=default_home,
        adapters={"primary": object()},
        _profile_adapters=secondaries or {},
    )


def test_cron_runtime_specs_cover_primary_once_and_each_served_profile(tmp_path):
    from gateway.run import _cron_runtime_specs

    default_home = tmp_path / ".hermes"
    ops_adapters = {"discord": object()}
    runner = _runner(
        default_home,
        secondaries={"ops": ops_adapters, "default": {"duplicate": object()}},
    )

    specs = _cron_runtime_specs(runner)

    assert [spec.profile for spec in specs] == ["default", "ops"]
    assert specs[0].home == default_home.resolve()
    assert specs[0].adapters is runner.adapters
    assert specs[1].home == (default_home / "profiles" / "ops").resolve()
    assert specs[1].adapters is ops_adapters


def test_profile_scoped_cron_provider_uses_profile_home_and_secrets(monkeypatch, tmp_path):
    from agent.secret_scope import get_secret
    from gateway.run import _start_profile_scoped_cron_provider
    from hermes_constants import get_hermes_home

    default_home = tmp_path / ".hermes"
    ops_home = default_home / "profiles" / "ops"
    ops_home.mkdir(parents=True)
    (ops_home / ".env").write_text("MULTIPLEX_SCOPE_SENTINEL=ops-value\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setenv("MULTIPLEX_SCOPE_SENTINEL", "wrong-process-value")
    observed = {}

    class Provider:
        def start(self, stop_event, **kwargs):
            observed["home"] = get_hermes_home()
            observed["secret"] = get_secret("MULTIPLEX_SCOPE_SENTINEL")
            observed["adapters"] = kwargs["adapters"]

    adapters = {"discord": object()}
    _start_profile_scoped_cron_provider(
        Provider(),
        object(),
        profile_home=ops_home,
        scope_secrets=True,
        adapters=adapters,
    )

    assert observed == {
        "home": ops_home.resolve(),
        "secret": "ops-value",
        "adapters": adapters,
    }


def test_execution_ledgers_follow_concurrent_profile_scopes(tmp_path):
    import cron.executions as executions
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    # Exercise the production dynamic-path branch, not the public monkeypatch seam.
    executions.EXECUTIONS_FILE = executions._IMPORT_EXECUTIONS_FILE
    homes = [tmp_path / "default", tmp_path / "profiles" / "ops"]

    def create(home: Path, job_id: str) -> str:
        token = set_hermes_home_override(home)
        try:
            return executions.create_execution(job_id, source="builtin")["job_id"]
        finally:
            reset_hermes_home_override(token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, homes, ("default-job", "ops-job")))

    assert results == ["default-job", "ops-job"]
    for home, expected in zip(homes, results):
        token = set_hermes_home_override(home)
        try:
            assert [row["job_id"] for row in executions.list_executions()] == [expected]
        finally:
            reset_hermes_home_override(token)


def test_kanban_singleton_owner_uses_secondary_profile_config_and_defaults(tmp_path):
    from gateway.run import _resolve_kanban_runtime_owner

    default_home = tmp_path / ".hermes"
    ops_home = default_home / "profiles" / "ops"
    default_home.mkdir(parents=True)
    ops_home.mkdir(parents=True)
    (default_home / "config.yaml").write_text("{}\n", encoding="utf-8")
    raw = {
        "kanban": {
            "discord_mirror": {
                "enabled": True,
                "board": "operations",
                "forum_channel_id": "ops-forum",
            }
        },
        "discord": {
            "kanban_reply_inbox": {
                "enabled": True,
                "forum_channel_ids": ["ops-forum"],
                "board_slug": "operations",
            }
        },
    }
    (ops_home / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    runner = _runner(default_home, secondaries={"ops": {"discord": object()}})

    owner = _resolve_kanban_runtime_owner(runner)

    assert owner is not None
    assert owner.profile == "ops"
    assert owner.home == ops_home.resolve()
    assert owner.mirror_config.board == "operations"
    assert owner.mirror_config.token_env_path == ops_home.resolve() / ".env"
    assert owner.inbox_config.board_slug == "operations"


def test_kanban_singleton_rejects_multiple_enabled_profile_owners(tmp_path):
    from gateway.errors import MultiplexConfigError
    from gateway.run import _resolve_kanban_runtime_owner

    default_home = tmp_path / ".hermes"
    ops_home = default_home / "profiles" / "ops"
    for home, board in ((default_home, "default-board"), (ops_home, "ops-board")):
        home.mkdir(parents=True, exist_ok=True)
        raw = {
            "kanban": {
                "discord_mirror": {
                    "enabled": True,
                    "board": board,
                    "forum_channel_id": f"{board}-forum",
                }
            }
        }
        (home / "config.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(MultiplexConfigError, match="default, ops"):
        _resolve_kanban_runtime_owner(
            _runner(default_home, secondaries={"ops": {"discord": object()}})
        )


def test_gateway_state_served_profiles_is_validated_coverage_truth(monkeypatch, tmp_path):
    from hermes_cli import profiles

    default_home = tmp_path / ".hermes"
    runtime = {"served_profiles": ["default", "ops"]}
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr("gateway.status.read_runtime_status", lambda path: runtime)
    monkeypatch.setattr(
        "gateway.status.get_runtime_status_running_pid",
        lambda state, expected_home: 4321 if state is runtime and expected_home == default_home else None,
    )

    coverage = profiles.get_multiplex_gateway_coverage("ops")

    assert coverage is not None
    assert coverage.pid == 4321
    assert coverage.served_profiles == ("default", "ops")
    assert profiles.get_multiplex_gateway_coverage("missing") is None


def test_concurrent_profile_cron_secret_refresh_is_context_local(monkeypatch, tmp_path):
    from agent.secret_scope import get_secret, set_multiplex_active
    from cron import scheduler
    from gateway.run import _start_profile_scoped_cron_provider
    from tools.environments.local import _sanitize_subprocess_env

    homes = {
        "default": tmp_path / ".hermes",
        "ops": tmp_path / ".hermes" / "profiles" / "ops",
    }
    for profile, home in homes.items():
        home.mkdir(parents=True, exist_ok=True)
        (home / ".env").write_text(
            f"MULTIPLEX_CRON_CREDENTIAL={profile}-credential\n", encoding="utf-8"
        )
    monkeypatch.setenv("MULTIPLEX_CRON_CREDENTIAL", "process-default-credential")
    barrier = threading.Barrier(2)
    observed: dict[str, tuple[str | None, str]] = {}

    class Provider:
        def __init__(self, profile: str):
            self.profile = profile

        def start(self, stop_event, **kwargs):
            scheduler._refresh_cron_runtime_secrets()
            barrier.wait(timeout=5)
            direct = get_secret("MULTIPLEX_CRON_CREDENTIAL")
            child_env = _sanitize_subprocess_env(os.environ.copy())
            child = subprocess.run(
                [sys.executable, "-c", "import os; print(os.getenv('MULTIPLEX_CRON_CREDENTIAL', ''))"],
                check=True,
                capture_output=True,
                text=True,
                env=child_env,
            ).stdout.strip()
            observed[self.profile] = (direct, child)

    set_multiplex_active(True)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    _start_profile_scoped_cron_provider,
                    Provider(profile),
                    object(),
                    profile_home=home,
                    scope_secrets=True,
                )
                for profile, home in homes.items()
            ]
            for future in futures:
                future.result(timeout=10)
    finally:
        set_multiplex_active(False)

    assert observed == {
        "default": ("default-credential", "default-credential"),
        "ops": ("ops-credential", "ops-credential"),
    }
    assert os.environ["MULTIPLEX_CRON_CREDENTIAL"] == "process-default-credential"


def test_multiplex_cron_refresh_reapplies_profile_external_secret_source(monkeypatch, tmp_path):
    from agent.secret_scope import get_secret, set_multiplex_active
    from agent.secret_sources.base import FetchResult, SecretSource
    from agent.secret_sources import registry
    from cron import scheduler
    from gateway.run import _profile_runtime_scope
    from tools.environments.local import _sanitize_subprocess_env

    profile_home = tmp_path / ".hermes" / "profiles" / "ops"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump({"secrets": {"sources": ["rotating"], "rotating": {"enabled": True}}}),
        encoding="utf-8",
    )
    rotated = {"value": "external-v1"}

    class RotatingSource(SecretSource):
        name = "rotating"
        label = "Rotating test source"

        def fetch(self, cfg, home_path):
            assert home_path == profile_home
            return FetchResult(secrets={"MULTIPLEX_EXTERNAL_CREDENTIAL": rotated["value"]})

    monkeypatch.setattr(registry, "_SOURCES", {"rotating": RotatingSource()})
    monkeypatch.setattr(registry, "_BUILTINS_LOADED", True)
    monkeypatch.setenv("MULTIPLEX_EXTERNAL_CREDENTIAL", "process-default-credential")

    set_multiplex_active(True)
    try:
        with _profile_runtime_scope(profile_home):
            scheduler._refresh_cron_runtime_secrets()
            assert get_secret("MULTIPLEX_EXTERNAL_CREDENTIAL") == "external-v1"

            rotated["value"] = "external-v2"
            scheduler._refresh_cron_runtime_secrets()
            child_env = _sanitize_subprocess_env(os.environ.copy())
            assert get_secret("MULTIPLEX_EXTERNAL_CREDENTIAL") == "external-v2"
            assert child_env["MULTIPLEX_EXTERNAL_CREDENTIAL"] == "external-v2"
    finally:
        set_multiplex_active(False)

    assert os.environ["MULTIPLEX_EXTERNAL_CREDENTIAL"] == "process-default-credential"


def test_nonmultiplex_cron_secret_refresh_keeps_legacy_dotenv(monkeypatch, tmp_path):
    from agent.secret_scope import set_multiplex_active
    from cron import scheduler

    calls = []
    monkeypatch.setattr("hermes_cli.env_loader.reset_secret_source_cache", lambda: calls.append("reset"))
    monkeypatch.setattr(
        "hermes_cli.env_loader.load_hermes_dotenv",
        lambda *, hermes_home: calls.append(Path(hermes_home).resolve()),
    )
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)

    set_multiplex_active(False)
    scheduler._refresh_cron_runtime_secrets()

    assert calls == ["reset", tmp_path.resolve()]


def test_profile_qualified_cron_state_and_pools_are_concurrent(monkeypatch, tmp_path):
    from cron import scheduler
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    scheduler._shutdown_parallel_pool()
    homes = [tmp_path / "default", tmp_path / "profiles" / "ops"]
    barrier = threading.Barrier(2)

    def enter(home: Path, workers: int):
        token = set_hermes_home_override(home)
        try:
            scheduler._register_running_job("same-id")
            pool = scheduler._get_parallel_pool(workers)
            barrier.wait(timeout=5)
            submitted = pool.submit(lambda: str(home)).result(timeout=5)
            visible = scheduler.get_running_job_ids()
            return submitted, visible
        finally:
            scheduler._release_running_job("same-id")
            reset_hermes_home_override(token)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(enter, homes, (1, 3)))
    finally:
        scheduler._shutdown_parallel_pool()

    assert results == [
        (str(homes[0]), frozenset({"same-id"})),
        (str(homes[1]), frozenset({"same-id"})),
    ]


def test_shutdown_marks_each_running_cron_job_in_its_profile_context(monkeypatch, tmp_path):
    from cron import scheduler
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

    homes = [tmp_path / "default", tmp_path / "profiles" / "ops"]
    marked = []
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, reason: marked.append((get_hermes_home().resolve(), job_id, success)),
    )
    for home in homes:
        token = set_hermes_home_override(home)
        try:
            scheduler._register_running_job("same-id")
        finally:
            reset_hermes_home_override(token)

    try:
        assert scheduler.get_running_job_count() == 2
        assert scheduler.mark_running_jobs_interrupted("shutdown") == ["same-id", "same-id"]
    finally:
        for home in homes:
            token = set_hermes_home_override(home)
            try:
                scheduler._release_running_job("same-id")
            finally:
                reset_hermes_home_override(token)

    assert sorted(marked) == sorted((home.resolve(), "same-id", False) for home in homes)


def test_multiplex_kanban_owner_missing_token_never_uses_process_default(monkeypatch, tmp_path):
    from plugins.platforms.discord.kanban_mirror.discord_client import load_discord_token

    ops_env = tmp_path / "profiles" / "ops" / ".env"
    ops_env.parent.mkdir(parents=True)
    ops_env.write_text("OTHER_SECRET=ops-only\n", encoding="utf-8")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "default-profile-token")

    assert load_discord_token(ops_env, allow_process_fallback=False) == ""
    assert load_discord_token(ops_env) == "default-profile-token"


@pytest.mark.parametrize(
    ("multiplex_profiles", "expected_process_fallback"),
    [(True, False), (False, True)],
)
def test_gateway_kanban_runtime_token_fallback_matches_multiplex_mode(
    monkeypatch, tmp_path, multiplex_profiles, expected_process_fallback
):
    from plugins.platforms.discord.kanban_mirror.runtime import DiscordKanbanMirrorRuntimeMixin

    calls = []

    async def fake_daemon(is_running, *, allow_process_token_fallback=True):
        calls.append(allow_process_token_fallback)

    class Supervisor:
        def start(self, name, factory):
            asyncio.run(factory())

    host = object.__new__(DiscordKanbanMirrorRuntimeMixin)
    host._kanban_mirror_supervisor = Supervisor()
    host._running = True
    host.config = SimpleNamespace(multiplex_profiles=multiplex_profiles)
    host._get_kanban_runtime_owner = lambda: SimpleNamespace(
        home=tmp_path,
        mirror_config=SimpleNamespace(enabled=True),
    )
    host._start_kanban_router_runtime = lambda: None
    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.daemon.run_mirror_daemon",
        fake_daemon,
    )

    host._start_discord_kanban_mirror_runtime()

    assert calls == [expected_process_fallback]


def test_nonmultiplex_startup_clears_stale_served_profiles(monkeypatch, tmp_path):
    from gateway import status
    from gateway.run import GatewayRunner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    status.write_runtime_status(
        gateway_state="running",
        served_profiles=["default", "ops"],
    )
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)

    assert asyncio.run(runner._start_secondary_profile_adapters()) == 0

    assert status.read_runtime_status()["served_profiles"] == []


def test_relay_only_secondary_is_deliberately_served_by_shared_ingress(monkeypatch, tmp_path):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters = {Platform.RELAY: SimpleNamespace(_running=True)}
    runner._profile_adapters = {}
    runner._served_profiles = {"default"}
    profile_cfg = GatewayConfig(multiplex_profiles=True)
    profile_cfg.platforms = {Platform.RELAY: PlatformConfig(enabled=True)}
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: profile_cfg)

    connected = asyncio.run(
        runner._start_one_profile_adapters(
            "ops", tmp_path / ".hermes" / "profiles" / "ops", {}
        )
    )

    assert connected == 0
    assert runner._profile_adapters["ops"] == {}
    assert runner._served_profiles == {"default", "ops"}


def test_failed_secondary_profile_is_not_served_or_scheduled(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner, _cron_runtime_specs

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner._gateway_profile_name = "default"
    runner._gateway_profile_home = tmp_path / ".hermes"
    runner.adapters = {"primary": object()}
    runner._profile_adapters = {}
    runner._served_profiles = {"default"}
    runner.pairing_stores = {}
    runner._adapter_credential_fingerprint = lambda adapter: None

    async def fail_all(profile_name, profile_home, claimed):
        runner._profile_adapters.setdefault(profile_name, {})
        return 0

    runner._start_one_profile_adapters = fail_all
    writes = []
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [("ops", tmp_path / ".hermes" / "profiles" / "ops")],
    )
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kw: writes.append(kw))

    assert asyncio.run(runner._start_secondary_profile_adapters()) == 0

    assert runner._served_profiles == {"default"}
    assert "ops" not in runner._profile_adapters
    assert writes[-1]["served_profiles"] == ["default"]
    assert [spec.profile for spec in _cron_runtime_specs(runner)] == ["default"]
