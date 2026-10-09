"""Exercise real result/persistence APIs using only synthetic backend output."""

import json
import time
from pathlib import Path

import pytest

from tools import process_registry as pr
from tools import terminal_tool as terminal


SECRET = "fictional" + "OpaqueValue976413"
OUTPUT = f"42 runner --setenv=DEMO_TOKEN={SECRET} MODE=normal ActiveState=active"
LONG_OUTPUT = 'DEMO_TOKEN="opaqueFirst' + 'x' * 4000 + '\nopaqueSecond"\nMODE=normal ActiveState=active'


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)


def assert_safe(text):
    assert SECRET not in text
    assert "opaqueFirst" not in text
    assert "opaqueSecond" not in text
    assert "MODE=normal" in text
    assert "ActiveState=active" in text


def persist_and_check(tmp_path, content):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "synthetic.db")
    try:
        db.create_session("synthetic-diagnostics", source="cli")
        db.append_message("synthetic-diagnostics", role="tool", content=content)
        messages = db.get_messages("synthetic-diagnostics")
        assert_safe(messages[-1]["content"])
    finally:
        db.close()


@pytest.mark.parametrize("output", [OUTPUT, LONG_OUTPUT])
def test_foreground_result_raw_spill_and_persistence(monkeypatch, tmp_path, output):
    monkeypatch.setattr("tools.tool_output_limits.get_max_bytes", lambda: 1000)
    spill = tmp_path / "synthetic-output.log"
    spill.write_text(output, encoding="utf-8")

    class SyntheticBackend:
        env = {}

        def execute(self, command, **kwargs):
            return {"output": output, "returncode": 0,
                    "output_total_chars": len(output), "full_output_path": str(spill)}

    monkeypatch.setattr(terminal, "_active_environments", {"default": SyntheticBackend()})
    monkeypatch.setattr(terminal, "_last_activity", {"default": time.time()})
    monkeypatch.setattr(terminal, "_task_env_overrides", {})
    monkeypatch.setattr(terminal, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal, "_get_env_config", lambda: {
        "env_type": "local", "cwd": str(tmp_path), "timeout": 1,
        "host_cwd": None, "modal_mode": "auto", "docker_image": "",
        "singularity_image": "", "modal_image": "", "daytona_image": "",
    })
    # Only the fake backend receives this command; no process inspection runs.
    result = json.loads(terminal.terminal_tool(command="ps aux"))
    assert result["exit_code"] == 0
    assert_safe(result["output"])
    assert Path(result["full_output_path"]) == spill
    assert_safe(spill.read_text(encoding="utf-8"))
    persist_and_check(tmp_path, json.dumps(result))


@pytest.mark.parametrize("action", ["poll", "log", "wait"])
@pytest.mark.parametrize("output", [OUTPUT, LONG_OUTPUT])
def test_background_model_result_and_persistence(monkeypatch, tmp_path, action, output):
    registry = pr.ProcessRegistry()
    session = pr.ProcessSession(
        id="proc_synthetic", command="/bin/systemctl status demo", task_id="synthetic",
        started_at=time.time(), exited=True, exit_code=0, output_buffer=output,
    )
    registry._finished[session.id] = session
    monkeypatch.setattr(pr, "process_registry", registry)
    result = pr._handle_process({"action": action, "session_id": session.id, "limit": 2})
    assert_safe(result)
    persist_and_check(tmp_path, result)
    # Retention inside the registry is a separate, existing contract.
    assert session.output_buffer == output


@pytest.mark.parametrize("output", [OUTPUT, LONG_OUTPUT])
def test_background_completion(monkeypatch, tmp_path, output):
    registry = pr.ProcessRegistry()
    session = pr.ProcessSession(
        id="proc_synthetic", command="journalctl -u demo", task_id="synthetic",
        started_at=time.time(), exited=True, exit_code=0, output_buffer=output,
    )
    session.notify_on_complete = True
    registry._running[session.id] = session
    registry._move_to_finished(session)
    notifications = registry.drain_notifications()
    assert len(notifications) == 1
    event, text = notifications[0]
    assert_safe(json.dumps(event))
    assert_safe(text)
    persist_and_check(tmp_path, text)


def persist(tmp_path, text):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "synthetic-review.db")
    try:
        db.create_session("synthetic-review", source="cli")
        db.append_message("synthetic-review", role="tool", content=text)
        saved = db.get_messages("synthetic-review")[-1]["content"]
        assert saved == text
        return saved
    finally:
        db.close()


@pytest.mark.parametrize("action", ["poll", "log", "wait", "list", "kill", "completion"])
def test_command_field_and_persistence(action, monkeypatch, tmp_path):
    from tools import process_registry as pr
    secret = "fictionalOpaqueValue976413"
    registry = pr.ProcessRegistry()
    session = pr.ProcessSession(
        id="proc_review_synthetic", command=f"systemd-run --setenv=DEMO_TOKEN={secret} /usr/bin/ps aux",
        task_id="review-synthetic", started_at=time.time(), exited=True, exit_code=0,
        output_buffer="MODE=normal", notify_on_complete=True,
    )
    monkeypatch.setattr(pr, "process_registry", registry)
    if action == "completion":
        registry._running[session.id] = session
        registry._move_to_finished(session)
        result = json.dumps(registry.drain_notifications())
    else:
        registry._finished[session.id] = session
        result = pr._handle_process({"action": action, "session_id": session.id}, task_id="review-synthetic")
    saved = persist(tmp_path, result)
    assert secret not in saved


@pytest.mark.parametrize("policy", ["diagnostic", "source", "opt_out"])
@pytest.mark.parametrize("capture", ["complete", "complete_marker", "absent", "missing", "capped"])
def test_real_collector_preview_vs_spill(monkeypatch, tmp_path, capture, policy):
    from tools import terminal_tool as terminal
    from tools.environments.base import _BoundedOutputCollector, BaseEnvironment
    marker = "opaqueTail976413"
    output = "benign\n" * 100 + 'DEMO_TOKEN="' + "x" * 3000 + marker + '"\nMODE=normal'
    cwd_marker = "__HERMES_CWD_synthetic__"
    if capture == "complete_marker":
        output += f"\n{cwd_marker}{tmp_path}{cwd_marker}\n"
    spill = tmp_path / "synthetic-review-spill.log"
    collector = _BoundedOutputCollector(1000, spill_path=None if capture == "absent" else spill)
    if capture == "capped":
        monkeypatch.setattr(collector, "_SPILL_CAP_CHARS", 2000)
    collector.append(output)
    captured = BaseEnvironment._finalize_wait_result(collector, collector.render(), 0)
    if capture == "complete_marker":
        from types import SimpleNamespace
        BaseEnvironment._extract_cwd_from_output(
            SimpleNamespace(_cwd_marker=cwd_marker), captured
        )
    if capture == "missing":
        spill.unlink()
    assert marker in captured["output"] and "DEMO_TOKEN=" not in captured["output"]

    class SyntheticBackend:
        env = {}
        _cwd_marker = cwd_marker
        def execute(self, command, **kwargs):
            assert kwargs["bounded_capture"] is True
            return captured

    monkeypatch.setattr(terminal, "_active_environments", {"default": SyntheticBackend()})
    monkeypatch.setattr(terminal, "_last_activity", {"default": time.time()})
    monkeypatch.setattr(terminal, "_task_env_overrides", {})
    monkeypatch.setattr(terminal, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal, "_get_env_config", lambda: {
        "env_type": "local", "cwd": str(tmp_path), "timeout": 1,
        "host_cwd": None, "modal_mode": "auto", "docker_image": "",
        "singularity_image": "", "modal_image": "", "daytona_image": "",
    })
    monkeypatch.setattr("tools.tool_output_limits.get_max_bytes", lambda: 1000)
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", policy != "opt_out")
    command = "cat fixture.py" if policy == "source" else "ps aux"
    result = json.loads(terminal.terminal_tool(command=command))
    assert result["exit_code"] == 0
    assert cwd_marker not in result["output"]
    if policy != "diagnostic":
        if capture in ("complete", "complete_marker") or policy == "opt_out":
            assert marker in result["output"]
            assert "MODE=normal" in result["output"]
        else:
            assert "benign" in result["output"]
            assert "prefix only" in result["output"]
        persist(tmp_path, json.dumps(result))
        return
    if capture in ("complete", "complete_marker"):
        spill_text = Path(result["full_output_path"]).read_text(encoding="utf-8")
        assert marker not in spill_text
        assert "MODE=normal" in result["output"]
    elif capture == "capped":
        assert "incomplete" in result["output"].lower()
    else:
        assert "unavailable" in result["output"].lower()
    saved = persist(tmp_path, json.dumps(result))
    assert marker not in saved




def test_list_command_redacted_before_truncation(monkeypatch, tmp_path):
    from agent.redact import redact_terminal_output

    command = "runner " + "-v " * 50 + "--setenv=DEMO_TOKEN=" + "opaqueMiddle" * 30 + " --mode normal"
    registry = pr.ProcessRegistry()
    session = pr.ProcessSession(
        id="proc_synthetic", command=command, task_id="synthetic",
        started_at=time.time(), exited=True, exit_code=0,
    )
    registry._finished[session.id] = session
    monkeypatch.setattr(pr, "process_registry", registry)
    result = pr._handle_process({"action": "list"}, task_id="synthetic")
    saved = persist(tmp_path, result)
    assert registry.list_sessions(task_id="synthetic")[0]["command"] == redact_terminal_output(command)[:200]
    shown = json.loads(saved)["processes"][0]["command"]
    assert " --mode " in shown
    assert "opaqueMiddle" not in shown
    assert len(shown) <= 200


def test_process_command_opt_out_and_source_contract(monkeypatch):
    source = 'cat fixture.py # MAX_TOKENS=100 DEMO_TOKEN="example value"'
    assert pr._redact_process_result({"command": source})["command"] == source
    command = f"runner --setenv=DEMO_TOKEN={SECRET}"
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
    assert pr._redact_process_result({"command": command})["command"] == command


def _run_status_result(monkeypatch, tmp_path, captured, enabled=True, cwd_marker=None, command="ps aux"):
    from tools import terminal_tool as terminal
    hook_inputs = []
    class SyntheticBackend:
        env = {}
        _cwd_marker = cwd_marker
        def execute(self, command, **kwargs):
            assert kwargs["bounded_capture"] is True
            return captured
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", enabled)
    monkeypatch.setattr(terminal, "_active_environments", {"default": SyntheticBackend()})
    monkeypatch.setattr(terminal, "_last_activity", {"default": time.time()})
    monkeypatch.setattr(terminal, "_task_env_overrides", {})
    monkeypatch.setattr(terminal, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal, "_get_env_config", lambda: {
        "env_type": "local", "cwd": str(tmp_path), "timeout": 1,
        "host_cwd": None, "modal_mode": "auto", "docker_image": "",
        "singularity_image": "", "modal_image": "", "daytona_image": "",
    })
    monkeypatch.setattr("tools.tool_output_limits.get_max_bytes", lambda: 1000)
    def hook(name, **kwargs):
        if name == "transform_terminal_output":
            hook_inputs.append(kwargs["output"])
            return [kwargs["output"] + '\nHOOK=kept DEMO_TOKEN="fictionalHookSecret976413"']
        return []
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", hook)
    result = json.loads(terminal.terminal_tool(command=command))
    assert len(hook_inputs) == 1
    assert "HOOK=kept" in result["output"]
    if enabled:
        assert "fictionalHookSecret976413" not in result["output"]
    return result, hook_inputs[0]


@pytest.mark.parametrize("capture,enabled", [("complete", True), ("absent", True), ("missing", True), ("capped", True), ("complete", False)])
@pytest.mark.parametrize("suffix,code", [("\n[Command interrupted]",130), ("\n[Command timed out after 7s]",124)])
def test_status_suffix_survives_reconstruction(monkeypatch, tmp_path, capture, enabled, suffix, code):
    from tools.environments.base import _BoundedOutputCollector, BaseEnvironment
    spill = tmp_path / "fictional-spill.log" if capture != "absent" else None
    collector = _BoundedOutputCollector(1000, spill_path=spill)
    if capture == "capped":
        monkeypatch.setattr(collector, "_SPILL_CAP_CHARS", 2000)
    collector.append("benign\n" * 100 + 'DEMO_TOKEN="' + "x" * 3000 + 'opaqueUnknownTail976413"\n')
    captured = BaseEnvironment._finalize_wait_result(collector, collector.render(suffix=suffix), code)
    assert suffix in captured["output"]
    if capture == "missing":
        spill.unlink()
    result, seen = _run_status_result(monkeypatch, tmp_path, captured, enabled=enabled)
    assert result["exit_code"] == code
    assert suffix in seen, "backend status suffix was dropped before plugin hook"
    assert suffix in result["output"], "backend status suffix was dropped from result"


    assert len(seen) <= 1000
    if enabled:
        assert "opaqueUnknownTail976413" not in seen + result["output"]
    assert suffix in json.loads(persist(tmp_path, json.dumps(result)))["output"]


@pytest.mark.parametrize("capture", ["complete", "absent", "capped"])
def test_stdin_status_metadata_survives_reconstruction(monkeypatch, tmp_path, capture):
    from tools.environments.base import _BoundedOutputCollector, BaseEnvironment

    collector = _BoundedOutputCollector(
        1000, spill_path=None if capture == "absent" else tmp_path / "stdin-spill.log"
    )
    if capture == "capped":
        monkeypatch.setattr(collector, "_SPILL_CAP_CHARS", 2000)
    collector.append("benign\n" * 700)
    captured = BaseEnvironment._finalize_wait_result(collector, collector.render(), 0)
    error = 'synthetic encode failure DEMO_TOKEN="fictionalStdinSecret976413"'
    captured["stdin_error"] = error
    captured["output"] += f"\n[stdin write failed: {error}]"
    result, seen = _run_status_result(monkeypatch, tmp_path, captured)
    assert "[stdin write failed: synthetic encode failure" in seen
    assert "[stdin write failed: synthetic encode failure" in result["output"]
    assert "fictionalStdinSecret976413" not in seen + result["output"]
    assert len(seen) <= 1000


@pytest.mark.parametrize("suffix,code", [
    ("\n[Command interrupted]", 0),
    ("\n[Command timed out after 7s]", 130),
    ("\n[Command timed out after opaqueUnknownTail976413s]", 124),
    ("\n[Command interrupted]\nopaqueUnknownTail976413", 130),
    ("\n[stdin write failed: opaqueUnknownTail976413]", 0),
])
def test_unknown_status_tail_is_not_restored(monkeypatch, tmp_path, suffix, code):
    from tools.environments.base import _BoundedOutputCollector, BaseEnvironment

    collector = _BoundedOutputCollector(1000)
    collector.append("benign\n" * 700)
    captured = BaseEnvironment._finalize_wait_result(
        collector, collector.render(suffix=suffix), code
    )
    result, seen = _run_status_result(monkeypatch, tmp_path, captured)
    assert suffix not in seen
    assert suffix not in result["output"]
    assert "opaqueUnknownTail976413" not in seen + result["output"]
