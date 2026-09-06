import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import shell_hooks
from hermes_cli import plugins
from run_agent import AIAgent


ROOT = Path(__file__).resolve().parents[1]
CONSUMER = ROOT / "scripts" / "notify_fallback_alert.py"
PYTHON = Path("/home/brian/.hermes/hermes-agent/.venv/bin/python")
if not PYTHON.exists():
    PYTHON = Path(sys.executable)


def _write_config(home: Path, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(body, encoding="utf-8")


def _default_config(*, homes: list[str] | None = None) -> str:
    homes = homes or ["telegram", "discord", "slack", "sms"]
    blocks = []
    for name in homes:
        token_key = "api_key" if name == "sms" else "token"
        blocks.append(
            f"""  {name}:
    enabled: true
    {token_key}: {name}-token
    home_channel:
      platform: {name}
      chat_id: {name}-home
      name: {name} home
"""
        )
    return "platforms:\n" + "".join(blocks)


def _install_fake_hermes(
    bin_dir: Path,
    capture: Path,
    *,
    fail: str = "",
    responses: dict[str, str | None] | None = None,
) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "hermes"
    encoded_responses = json.dumps(responses or {})
    script.write_text(
        f"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
capture = Path({str(capture)!r})
responses = json.loads({encoded_responses!r})
capture.parent.mkdir(parents=True, exist_ok=True)
platform = sys.argv[sys.argv.index("--to") + 1] if "--to" in sys.argv else ""
row = {{
    "argv": sys.argv[1:],
    "platform": platform,
    "message": sys.argv[-1] if sys.argv else "",
    "hermes_home": os.environ.get("HERMES_HOME"),
    "hermes_profile": os.environ.get("HERMES_PROFILE"),
    "session_platform": os.environ.get("HERMES_SESSION_PLATFORM"),
    "ambient_telegram_token": os.environ.get("TELEGRAM_BOT_TOKEN"),
}}
with capture.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(row, ensure_ascii=False) + "\\n")
if platform == {fail!r}:
    print("forced failure for " + platform, file=sys.stderr)
    raise SystemExit(7)
response = responses.get(platform, '{{"success": true}}')
if response is not None:
    print(response)
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    active_home = root / "profiles" / "ang"
    capture = tmp_path / "captured.jsonl"
    fake_bin = tmp_path / "bin"
    _install_fake_hermes(fake_bin, capture)
    _write_config(root, _default_config())
    _write_config(
        active_home,
        f"""hooks:
  fallback_activated:
    - command: {PYTHON} {CONSUMER}
      timeout: 90
fallback_providers: []
fallback_model: []
hooks_auto_accept: false
""",
    )
    monkeypatch.setenv("HERMES_HOME", str(active_home))
    monkeypatch.setenv("HERMES_PROFILE", "ang")
    monkeypatch.setenv("HOME", str(tmp_path / "safe-home"))
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "ambient-ang-token")
    monkeypatch.delenv("HERMES_SAFE_MODE", raising=False)
    monkeypatch.delenv("HERMES_ACCEPT_HOOKS", raising=False)
    shell_hooks.reset_for_tests()
    manager = plugins.get_plugin_manager()
    old_hooks = {name: list(callbacks) for name, callbacks in manager._hooks.items()}
    manager._hooks.clear()
    yield root, active_home, capture
    manager._hooks.clear()
    manager._hooks.update(old_hooks)
    shell_hooks.reset_for_tests()


def _registered_hook_config() -> dict:
    return {
        "hooks": {
            "fallback_activated": [
                {"command": f"{PYTHON} {CONSUMER}", "timeout": 90}
            ]
        },
        "hooks_auto_accept": False,
    }


def _read_capture(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _fallback_event(**extra):
    payload = {
        "session_id": "sess-test",
        "old_provider": "openrouter",
        "old_model": "primary-model",
        "provider": "xai-oauth",
        "model": "grok-fixture-model",
        "fallback_provider": "xai-oauth",
        "fallback_model": "grok-fixture-model",
        "stage": "mid_turn",
        "reason": "rate_limit",
        "platform": "cli",
    }
    payload.update(extra)
    plugins.invoke_hook("fallback_activated", **payload)


def test_registered_hook_fans_out_default_identity_and_preserves_ang_config(hook_env):
    root, active_home, capture = hook_env
    registered = shell_hooks.register_from_config(
        _registered_hook_config(), accept_hooks=True
    )
    assert [spec.event for spec in registered] == ["fallback_activated"]

    _fallback_event(profile_name="ang")

    rows = _read_capture(capture)
    assert sorted(row["platform"] for row in rows) == ["discord", "slack", "sms", "telegram"]
    assert all("--json" in row["argv"] for row in rows)
    assert all("--quiet" not in row["argv"] for row in rows)
    assert all(row["message"].startswith("🚨 High-priority Alert 🚨") for row in rows)
    assert all(len(row["message"]) <= 140 for row in rows)
    assert all(row["hermes_home"] == str(root) for row in rows)
    assert all(row["hermes_profile"] == "default" for row in rows)
    assert all(row["session_platform"] is None for row in rows)
    assert all(row["ambient_telegram_token"] is None for row in rows)

    active_cfg = (active_home / "config.yaml").read_text(encoding="utf-8")
    assert "fallback_providers: []" in active_cfg
    assert "fallback_model: []" in active_cfg
    assert "hooks_auto_accept: false" in active_cfg


def test_regular_api_hook_and_manual_switch_do_not_alert(hook_env):
    root, _, capture = hook_env
    shell_hooks.register_from_config(_registered_hook_config(), accept_hooks=True)

    plugins.invoke_hook(
        "post_api_request",
        session_id="sess",
        provider="xai-oauth",
        model="grok-fixture-model",
    )

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="primary",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    with patch.object(agent, "_create_openai_client", return_value=MagicMock()):
        agent.switch_model(
            "grok-fixture-model",
            "xai-oauth",
            api_key="new-key",
            base_url="https://api.x.ai/v1",
            api_mode="chat_completions",
        )

    assert _read_capture(capture) == []


def test_midturn_activation_emits_actual_fallback_event_for_grok(hook_env):
    root, _, capture = hook_env
    shell_hooks.register_from_config(_registered_hook_config(), accept_hooks=True)
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="primary",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": "xai-oauth", "model": "grok-fixture-model"}],
        )
    agent.client = MagicMock()
    fallback_client = SimpleNamespace(
        api_key="fb-key",
        base_url="https://api.x.ai/v1",
        _custom_headers={},
    )
    with (
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(fallback_client, "grok-fixture-model")),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
    ):
        assert agent._try_activate_fallback() is True

    rows = _read_capture(capture)
    assert rows
    assert all("grok-fixture-model" in row["message"] for row in rows)


def test_agent_init_activation_emits_event(hook_env):
    root, _, capture = hook_env
    shell_hooks.register_from_config(_registered_hook_config(), accept_hooks=True)
    fallback_client = SimpleNamespace(
        api_key="fb-key",
        base_url="https://fallback.invalid/v1",
        _custom_headers={},
    )
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            side_effect=[(None, None), (fallback_client, "fallback-model")],
        ),
    ):
        AIAgent(
            provider="missing-provider",
            model="primary",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": "fallback-provider", "model": "fallback-model"}],
        )

    rows = _read_capture(capture)
    assert rows
    assert all("fallback-provider/fallback-model" in row["message"] for row in rows)


def test_tui_and_gateway_runtime_fallback_emit_events(hook_env, monkeypatch):
    from hermes_cli.auth import AuthError
    import gateway.run as gateway_run
    import tui_gateway.server as tui_server

    root, _, capture = hook_env
    shell_hooks.register_from_config(_registered_hook_config(), accept_hooks=True)

    def tui_resolve(**kwargs):
        if kwargs.get("requested") == "openai-codex":
            raise AuthError("missing primary")
        return {"provider": kwargs["requested"], "api_key": "fb", "base_url": "https://fb.invalid/v1"}

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        tui_resolve,
    )
    monkeypatch.setattr(
        tui_server,
        "_load_fallback_model",
        lambda: [{"provider": "xai-oauth", "model": "grok-same-model"}],
    )
    result = tui_server._resolve_runtime_with_fallback(
        {"requested": "openai-codex", "target_model": "grok-same-model"}
    )
    assert result.used_fallback is True
    state_path = root / "state" / "fallback-alert-cooldown.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["until_epoch"] = time.time() - 0.01
    state_path.write_text(json.dumps(state), encoding="utf-8")

    def gateway_resolve(**kwargs):
        return {
            "provider": kwargs.get("requested"),
            "api_key": "fb",
            "base_url": "https://fb.invalid/v1",
            "api_mode": "chat_completions",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        gateway_resolve,
    )
    monkeypatch.setattr(
        gateway_run,
        "_hermes_home",
        Path(os.environ["HERMES_HOME"]),
    )
    _write_config(
        Path(os.environ["HERMES_HOME"]),
        """fallback_providers:
  - provider: slack-llm
    model: same-model
hooks_auto_accept: false
""",
    )
    assert gateway_run._try_resolve_fallback_provider()["model"] == "same-model"

    messages = [row["message"] for row in _read_capture(capture)]
    assert any("xai-oauth/grok-same-model" in msg for msg in messages)
    assert any("slack-llm/same-model" in msg for msg in messages)


def test_cli_runtime_fallback_emits_event(hook_env, monkeypatch):
    from hermes_cli.auth import AuthError
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    _, _, capture = hook_env
    shell_hooks.register_from_config(_registered_hook_config(), accept_hooks=True)

    def resolve(**kwargs):
        if kwargs.get("requested") == "openai-codex":
            raise AuthError("missing primary")
        return {
            "provider": kwargs.get("requested"),
            "model": "grok-cli-fallback",
            "api_key": "fb",
            "base_url": "https://fb.invalid/v1",
            "api_mode": "chat_completions",
            "credential_pool": None,
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", resolve)
    monkeypatch.setattr(
        "hermes_cli.model_switch.normalize_model_for_provider",
        lambda model, provider: model,
        raising=False,
    )

    class Harness(CLIAgentSetupMixin):
        requested_provider = "openai-codex"
        provider = "openai-codex"
        model = "primary"
        api_mode = "codex_responses"
        acp_command = None
        acp_args = []
        api_key = ""
        base_url = ""
        _explicit_api_key = ""
        _explicit_base_url = ""
        _fallback_model = [{"provider": "xai-oauth", "model": "grok-cli-fallback"}]
        _credential_pool = None
        session_id = "cli-session"
        agent = None
        _active_agent_route_signature = None

        def _normalize_model_for_provider(self, _provider):
            return False

    assert Harness()._ensure_runtime_credentials() is True
    messages = [row["message"] for row in _read_capture(capture)]
    assert any("xai-oauth/grok-cli-fallback" in msg for msg in messages)


def test_cooldown_boundary_and_suppressed_event_does_not_slide(hook_env):
    root, _, capture = hook_env
    shell_hooks.register_from_config(_registered_hook_config(), accept_hooks=True)

    _fallback_event(model="first-model", fallback_model="first-model")
    state_path = root / "state" / "fallback-alert-cooldown.json"
    first_state = json.loads(state_path.read_text(encoding="utf-8"))
    first_until = first_state["until_epoch"]

    _fallback_event(model="suppressed-model", fallback_model="suppressed-model")
    second_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert second_state["until_epoch"] == first_until
    assert len(_read_capture(capture)) == 4

    first_state["until_epoch"] = time.time() - 0.01
    state_path.write_text(json.dumps(first_state), encoding="utf-8")
    _fallback_event(model="after-boundary", fallback_model="after-boundary")
    third_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert third_state["until_epoch"] > first_until
    assert len(_read_capture(capture)) == 8


def test_no_destinations_fail_without_cooldown(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    active = root / "profiles" / "ops"
    _write_config(root, "platforms: {}\n")
    monkeypatch.setenv("HERMES_HOME", str(active))
    monkeypatch.setenv("HERMES_PROFILE", "ops")
    monkeypatch.setenv("HOME", str(tmp_path / "safe-home"))
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    payload = {
        "hook_event_name": "fallback_activated",
        "extra": {"provider": "xai-oauth", "model": "grok-fixture-model"},
    }
    result = subprocess.run(
        [str(PYTHON), str(CONSUMER)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "no enabled default-profile messaging home channels" in result.stderr
    assert not (root / "state" / "fallback-alert-cooldown.json").exists()


def test_partial_delivery_failure_is_nonzero_and_consumes_one_cooldown(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    active = root / "profiles" / "ang"
    capture = tmp_path / "captured.jsonl"
    fake_bin = tmp_path / "bin"
    _install_fake_hermes(fake_bin, capture, fail="slack")
    _write_config(root, _default_config(homes=["telegram", "slack"]))
    monkeypatch.setenv("HERMES_HOME", str(active))
    monkeypatch.setenv("HERMES_PROFILE", "ang")
    monkeypatch.setenv("HOME", str(tmp_path / "safe-home"))
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    payload = {
        "hook_event_name": "fallback_activated",
        "extra": {"provider": "xai-oauth", "model": "grok-fixture-model"},
    }
    result = subprocess.run(
        [str(PYTHON), str(CONSUMER)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "slack: exit 7" in result.stderr
    assert (root / "state" / "fallback-alert-cooldown.json").exists()
    rows = _read_capture(capture)
    assert sorted(row["platform"] for row in rows) == ["slack", "telegram"]


def test_skipped_send_json_is_rejected_despite_real_quiet_exit_semantics(tmp_path, monkeypatch):
    from hermes_cli.send_cmd import _emit_result

    assert _emit_result(
        json.dumps({"skipped": True}),
        json_mode=False,
        quiet=True,
    ) == 0

    root = tmp_path / "hermes"
    active = root / "profiles" / "ang"
    capture = tmp_path / "captured.jsonl"
    fake_bin = tmp_path / "bin"
    _install_fake_hermes(
        fake_bin,
        capture,
        responses={"telegram": json.dumps({"skipped": True})},
    )
    _write_config(root, _default_config(homes=["telegram"]))
    monkeypatch.setenv("HERMES_HOME", str(active))
    monkeypatch.setenv("HERMES_PROFILE", "ang")
    monkeypatch.setenv("HOME", str(tmp_path / "safe-home"))
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    payload = {
        "hook_event_name": "fallback_activated",
        "extra": {"provider": "xai-oauth", "model": "grok-fixture-model"},
    }

    result = subprocess.run(
        [str(PYTHON), str(CONSUMER)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "telegram: delivery skipped" in result.stderr
    rows = _read_capture(capture)
    assert rows[0]["argv"][:4] == ["send", "--to", "telegram", "--json"]
    assert "--quiet" not in rows[0]["argv"]


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("", "empty JSON result"),
        ("not-json", "invalid JSON result"),
        (json.dumps({"delivered": True}), "delivery not confirmed"),
    ],
)
def test_send_json_result_must_be_well_formed_affirmative(stdout, expected):
    from scripts import notify_fallback_alert as alert

    result = subprocess.CompletedProcess(
        args=["hermes"],
        returncode=0,
        stdout=stdout,
        stderr="",
    )

    assert alert._send_failure("telegram", result).startswith(f"telegram: {expected}")


def test_cooldown_lock_fails_closed_when_fcntl_is_unavailable(tmp_path, monkeypatch):
    from scripts import notify_fallback_alert as alert

    monkeypatch.setattr(alert, "fcntl", None)

    with pytest.raises(RuntimeError, match="cooldown locking requires fcntl"):
        alert._reserve_cooldown(tmp_path / "hermes", {"profile_name": "ang"})


def test_parallel_delivery_timeout_does_not_strand_other_destinations(tmp_path, monkeypatch):
    from scripts import notify_fallback_alert as alert

    fast_done = threading.Event()
    calls = []

    def fake_run_send(hermes_bin, *, root, platform, message):
        calls.append(platform)
        if platform == "slack":
            fast_done.wait(timeout=1)
            raise subprocess.TimeoutExpired(cmd=["hermes", "send"], timeout=0.1)
        fast_done.set()
        return subprocess.CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout=json.dumps({"success": True}),
            stderr="",
        )

    monkeypatch.setattr(alert, "_run_send", fake_run_send)
    started = time.monotonic()

    failures = alert._deliver_all(
        "hermes",
        root=tmp_path / "hermes",
        platforms=["slack", "telegram"],
        message="alert",
    )

    assert time.monotonic() - started < 0.5
    assert sorted(calls) == ["slack", "telegram"]
    assert failures == ["slack: timeout"]


def test_plugin_env_only_home_channel_is_included(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    active = root / "profiles" / "ang"
    capture = tmp_path / "captured.jsonl"
    fake_bin = tmp_path / "bin"
    _install_fake_hermes(fake_bin, capture)
    root.mkdir(parents=True, exist_ok=True)
    active.mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text(
        "NTFY_TOPIC=ops-alerts\nNTFY_HOME_CHANNEL=ops-home\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(active))
    monkeypatch.setenv("HERMES_PROFILE", "ang")
    monkeypatch.setenv("HOME", str(tmp_path / "safe-home"))
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    payload = {
        "hook_event_name": "fallback_activated",
        "extra": {"provider": "xai-oauth", "model": "grok-fixture-model"},
    }

    result = subprocess.run(
        [str(PYTHON), str(CONSUMER)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    rows = _read_capture(capture)
    assert [row["platform"] for row in rows] == ["ntfy"]
    assert rows[0]["hermes_home"] == str(root)
    assert rows[0]["hermes_profile"] == "default"


def test_simultaneous_cross_profile_processes_share_global_cooldown(tmp_path):
    root = tmp_path / "hermes"
    capture = tmp_path / "captured.jsonl"
    fake_bin = tmp_path / "bin"
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()
    barrier_consumer = tmp_path / "barrier_consumer.py"
    barrier_consumer.write_text(
        f"""#!/usr/bin/env python3
import os, subprocess, sys, time
from pathlib import Path

payload = sys.stdin.read()
barrier = Path(os.environ["BARRIER_DIR"])
ready = barrier / (os.environ["BARRIER_ID"] + ".ready")
release = barrier / "release"
ready.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 30
while not release.exists():
    if time.monotonic() > deadline:
        print("barrier release timed out", file=sys.stderr)
        raise SystemExit(98)
    time.sleep(0.01)
result = subprocess.run(
    [sys.executable, {str(CONSUMER)!r}],
    input=payload,
    text=True,
    capture_output=True,
    env=os.environ.copy(),
    check=False,
)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
raise SystemExit(result.returncode)
""",
        encoding="utf-8",
    )
    barrier_consumer.chmod(0o755)
    _install_fake_hermes(fake_bin, capture)
    _write_config(root, _default_config(homes=["telegram", "discord"]))
    env_base = {
        **os.environ,
        "HOME": str(tmp_path / "safe-home"),
        "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
    }
    procs = []
    for i in range(4):
        home = root / "profiles" / f"p{i}"
        home.mkdir(parents=True, exist_ok=True)
        env = {
            **env_base,
            "BARRIER_DIR": str(barrier_dir),
            "BARRIER_ID": f"p{i}",
            "HERMES_HOME": str(home),
            "HERMES_PROFILE": f"p{i}",
        }
        payload = json.dumps(
            {
                "hook_event_name": "fallback_activated",
                "extra": {
                    "provider": "xai-oauth",
                    "model": f"grok-fixture-model-{i}",
                    "profile_name": f"p{i}",
                },
            }
        )
        procs.append(
            (
                payload,
                subprocess.Popen(
                    [str(PYTHON), str(barrier_consumer)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                ),
            )
        )
    try:
        for payload, proc in procs:
            assert proc.stdin is not None
            proc.stdin.write(payload)
            proc.stdin.close()
            proc.stdin = None
        deadline = time.monotonic() + 30
        while len(list(barrier_dir.glob("*.ready"))) < len(procs):
            if time.monotonic() > deadline:
                raise TimeoutError("not all fallback alert contenders reached barrier")
            time.sleep(0.01)
        (barrier_dir / "release").write_text("go", encoding="utf-8")
        results = []
        for _, proc in procs:
            stdout, stderr = proc.communicate(timeout=90)
            results.append((stdout, stderr, proc.returncode))
    except Exception:
        for _, proc in procs:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)
        raise

    assert all(code == 0 for _, _, code in results)
    rows = _read_capture(capture)
    assert len(rows) == 2
    assert sorted(row["platform"] for row in rows) == ["discord", "telegram"]
    assert len({row["message"] for row in rows}) == 1
    assert all(row["hermes_home"] == str(root) for row in rows)
    state = json.loads((root / "state" / "fallback-alert-cooldown.json").read_text(encoding="utf-8"))
    assert state["model"] in rows[0]["message"]
