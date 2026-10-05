"""Terminal file receipts: synthetic supervisor responses, real private file I/O."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import profiles
from hermes_cli import kanban_external_process as ep


@pytest.fixture
def process_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(profiles, "get_profile_dir", lambda name: tmp_path)
    monkeypatch.setattr(kb, "kanban_home", lambda: tmp_path)
    release = tmp_path / "state" / "release-switches" / "release-1"
    release.mkdir(parents=True, mode=0o700)
    release.parent.chmod(0o700)
    release.parent.parent.chmod(0o700)
    output = release / "last-message.txt"
    props = dict(Id="item6-test.service", LoadState="loaded", Transient="yes",
                 InvocationID="a" * 32, ExecMainStartTimestampMonotonic=str(time.monotonic_ns() // 1000 - 1000000), ActiveState="active", SubState="running",
                 MainPID="424242", ExecMainPID="424242", ControlGroup="/test/executor.service", TasksCurrent="1", Result="success", ExecMainCode="0", ExecMainStatus="0",
                 Environment=f'HERMES_HOME={tmp_path}')
    def inspect(argv, **kwargs):
        assert argv[:3] == ["systemctl", "--user", "show"]
        return SimpleNamespace(returncode=0, stdout="\n".join(f"{k}={v}" for k, v in props.items()))
    monkeypatch.setattr(subprocess, "run", inspect)
    def process_identity(pid):
        if pid == os.getpid():
            return dict(pid=pid, start_ticks=1, pgrp=100, cgroup="/test/control.service")
        return dict(pid=pid, start_ticks=int(props["ExecMainStartTimestampMonotonic"]) * os.sysconf("SC_CLK_TCK") // 1_000_000,
                    pgrp=424242, cgroup="/test/executor.service")
    monkeypatch.setattr(ep, "_proc_identity", process_identity)
    conn = kb.connect(kb.board_db_path("default"))
    card = kb.create_task(conn, title="CLI process wait", assignee="ang", subscription_only=True,
                          provider_override="openai-codex", model_override="gpt-6.1-sol")
    run = kb.claim_task(conn, card).current_run_id
    ref = dict(kind="external_process", unit=props["Id"], invocation_id=props["InvocationID"],
               release_id="release-1", phase="source", candidate_sha="b" * 40,
               known_good_sha="c" * 40, result_basename=output.name)
    wait = dict(next_owner="default", executor="ang", expected_effect="Read terminal CLI result",
                result_ref=ref, resume_condition="result", recheck_at=int(time.time()) + 60)
    yield conn, card, run, wait, props, output
    conn.close()


def finish(props, status=0):
    props.update(ActiveState="inactive" if status == 0 else "failed", SubState="dead" if status == 0 else "failed",
                 MainPID="0", TasksCurrent="0", ExecMainCode="1", ExecMainStatus=str(status),
                 Result="success" if status == 0 else "exit-code")


def write_result(output):
    output.write_bytes(b"Deterministic test child output; not a model execution.\n")
    output.chmod(0o600)


@pytest.mark.parametrize("terminal", ["success", "failure"])
def test_terminal_released_cgroup_keeps_exact_invocation_receipt(process_wait, terminal):
    conn, card, run, wait, props, output = process_wait
    assert kb.schedule_task(conn, card, reason="independent service pending", expected_run_id=run, wait=wait)
    write_result(output)
    finish(props, 0 if terminal == "success" else 9)
    if terminal == "success":
        props.update(ActiveState="active", SubState="exited")
    props.update(ControlGroup="", TasksCurrent="[not set]")
    assert kb.reconcile_external_waits(conn) == ([card] if terminal == "success" else [])
    receipt = kb.get_run(conn, run).metadata["external_process_receipt"]
    assert receipt["verdict"] == ("process_exited" if terminal == "success" else "process_failed")
    assert kb.reconcile_external_waits(conn) == []


def test_live_output_waits_for_terminal_then_dispatches_once(process_wait, monkeypatch):
    conn, card, run, wait, props, output = process_wait
    assert kb.schedule_task(conn, card, reason="CLI owns pending result", expected_run_id=run, wait=wait)
    assert kb.get_run(conn, run).outcome == "scheduled"
    assert kb.get_run(conn, run).metadata["external_wait"] == wait
    write_result(output)
    assert kb.reconcile_external_waits(conn) == []
    finish(props)
    calls = []
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "normal")
    def spawn(task, workspace, **kwargs):
        calls.append((task.assignee, task.model_override, task.provider_override, task.subscription_only))
        return os.getpid()
    for _ in range(2):
        kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, board="default")
    assert calls == [("default", "gpt-6.1-sol", "openai-codex", True)]
    receipt = kb.get_run(conn, run).metadata["external_process_receipt"]
    assert receipt["result_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert receipt["verdict"] == "process_exited"
    assert kb.get_task(conn, card).consecutive_failures == 0
    assert kb.get_task(conn, card).block_recurrences == 0


@pytest.mark.parametrize("guard", ["invocation", "profile", "missing_unit", "not_transient", "terminal"])
def test_schedule_requires_live_bound_supervisor(process_wait, guard):
    conn, card, run, wait, props, output = process_wait
    if guard == "invocation":
        props["InvocationID"] = "d" * 32
    elif guard == "profile":
        props["Environment"] = "HERMES_HOME=/wrong-owner"
    elif guard == "missing_unit":
        props["LoadState"] = "not-found"
    elif guard == "not_transient":
        props["Transient"] = "no"
    else:
        finish(props)
    before = kb.list_events(conn, card)
    assert not kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
    assert kb.list_events(conn, card) == before


@pytest.mark.parametrize("guard", ["symlink", "public", "empty", "oversize", "missing", "fifo", "hardlink", "directory_symlink", "stale_invocation", "stale_file"])
def test_terminal_result_fails_closed(process_wait, guard):
    conn, card, run, wait, props, output = process_wait
    assert kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
    write_result(output)
    if guard == "symlink":
        other = output.with_name("other")
        output.rename(other)
        output.symlink_to(other)
    elif guard == "public":
        output.chmod(0o644)
    elif guard == "empty":
        output.write_bytes(b"")
    elif guard == "oversize":
        output.write_bytes(b"x" * (1024 * 1024 + 1))
    elif guard == "missing":
        output.unlink()
    elif guard == "fifo":
        output.unlink()
        os.mkfifo(output, 0o600)
    elif guard == "hardlink":
        os.link(output, output.with_name("alias"))
    elif guard == "directory_symlink":
        other = output.parent.with_name("other")
        output.parent.rename(other)
        output.parent.symlink_to(other, target_is_directory=True)
    elif guard == "stale_file":
        os.utime(output, (1, 1))
    else:
        props["InvocationID"] = "d" * 32
    finish(props)
    assert kb.reconcile_external_waits(conn) == []
    assert kb.get_task(conn, card).assignee == "ang"
    assert "external_process_receipt" not in kb.get_run(conn, run).metadata


@pytest.mark.parametrize("has_output", [True, False])
def test_crash_is_current_owner_failure_once_even_after_due(process_wait, has_output):
    conn, card, run, wait, props, output = process_wait
    assert kb.schedule_task(conn, card, reason="CLI result pending", expected_run_id=run, wait=wait)
    kb.reconcile_external_waits(conn, now=wait["recheck_at"] + 1)
    if has_output:
        write_result(output)
    finish(props, 9)
    assert kb.reconcile_external_waits(conn) == []
    receipt = kb.get_run(conn, run).metadata["external_process_receipt"]
    assert receipt["verdict"] == "process_failed" and receipt["exit_status"] == 9
    events = kb.list_events(conn, card)
    due = [e for e in events if e.kind == "external_wait_due"]
    assert len(due) == 2
    assert due[-1].payload["external_process_receipt"] == receipt
    assert kb.reconcile_external_waits(conn) == []
    assert kb.list_events(conn, card) == events
    task = kb.get_task(conn, card)
    assert task.assignee == "ang" and task.status == "scheduled"
    assert task.consecutive_failures == task.block_recurrences == 0


@pytest.mark.parametrize("guard", ["hold", "owner", "freshness", "estop", "parent"])
def test_process_result_preserves_dispatch_fences(process_wait, monkeypatch, guard):
    conn, card, run, wait, props, output = process_wait
    assert kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
    write_result(output)
    finish(props)
    if guard == "hold":
        with kb.write_txn(conn):
            kb._append_event(conn, card, "blocked", {"kind": "needs_input", "recurrences": 1})
    elif guard == "owner":
        kb.assign_task(conn, card, "default")
    elif guard == "freshness":
        kb.unblock_task(conn, card)
        kb.schedule_task(conn, card, reason="New operator schedule")
    elif guard == "estop":
        from agent import estop
        monkeypatch.setattr(estop, "is_engaged", lambda: True)
    else:
        parent = kb.create_task(conn, title="unfinished parent")
        kb.link_tasks(conn, parent, card)
    assert kb.reconcile_external_waits(conn) == []
    assert kb.get_task(conn, card).status == "scheduled"


@pytest.mark.parametrize("condition", ["accepted", "launched"])
def test_process_liveness_cannot_enable_owner_transfer(process_wait, condition):
    conn, card, run, wait, props, output = process_wait
    wait["resume_condition"] = condition
    before = kb.list_events(conn, card)
    with pytest.raises(ValueError, match="resume_condition must be result"):
        kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
    assert kb.list_events(conn, card) == before
    assert kb.get_task(conn, card).current_run_id == run


def test_detached_child_in_control_cgroup_is_not_independent_service(tmp_path, monkeypatch):
    """Real detached process is insufficient: it still shares the controller cgroup."""
    import sys
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"], start_new_session=True)
    try:
        actual = ep._proc_identity(child.pid)
        controller = ep._proc_identity(os.getpid())
        assert actual["cgroup"] == controller["cgroup"]
        props = dict(MainPID=str(child.pid), ExecMainPID=str(child.pid), ControlGroup=actual["cgroup"],
                     ExecMainStartTimestampMonotonic=str(actual["start_ticks"] * 1_000_000 // os.sysconf("SC_CLK_TCK")))
        with pytest.raises(ValueError, match="independent"):
            ep._live_identity(props)
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_surviving_service_children_prevent_result(process_wait):
    conn, card, run, wait, props, output = process_wait
    assert kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
    write_result(output)
    finish(props)
    props.update(ActiveState="active", SubState="exited", TasksCurrent="1")
    assert kb.reconcile_external_waits(conn) == []
    props["TasksCurrent"] = "0"
    assert kb.reconcile_external_waits(conn) == [card]


@pytest.mark.parametrize("key", ["release_id", "result_basename", "unit"])
def test_reference_rejects_traversal_before_mutation(process_wait, key):
    conn, card, run, wait, props, output = process_wait
    wait["result_ref"][key] = "../escape"
    before = kb.list_events(conn, card)
    with pytest.raises(ValueError):
        kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
    assert kb.list_events(conn, card) == before


def test_tool_handler_accepts_external_variant_and_preserves_metadata(process_wait, monkeypatch):
    from tools.kanban_tools import _handle_block
    conn, card, run, wait, props, output = process_wait
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", ('{"prior":"retain"}', run))
    conn.commit()
    monkeypatch.setenv("HERMES_KANBAN_TASK", card)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb._connection_main_db_path(conn)))
    result = json.loads(_handle_block(dict(reason="CLI owns pending result", kind="dependency", wait=wait)))
    assert result["status"] == "scheduled", result
    metadata = kb.get_run(conn, run).metadata
    assert metadata["prior"] == "retain" and metadata["external_wait"] == wait
    assert metadata["external_process_identity"]["pid"] == 424242


def test_hardened_root_and_no_output_text_in_ledger(process_wait, monkeypatch):
    conn, card, run, wait, props, output = process_wait
    original = os.open
    def hardened(path, *args, **kwargs):
        assert path != "/", "hardened host cannot open root"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", hardened)
    assert kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
    output.write_bytes(b"SECRET_SENTINEL_MUST_STAY_PRIVATE")
    output.chmod(0o600)
    finish(props)
    assert kb.reconcile_external_waits(conn) == [card]
    ledger = json.dumps(kb.get_run(conn, run).metadata) + str(kb.list_events(conn, card))
    assert "SECRET_SENTINEL" not in ledger and "result_preview" not in ledger


def test_foreign_executor_rejected_before_inspection(process_wait, monkeypatch):
    from hermes_cli import kanban_external_process as ep
    conn, card, run, wait, props, output = process_wait
    wait["executor"] = "default"  # initiating owner is ang
    def forbidden(*args, **kwargs):
        raise AssertionError("foreign owner inspection")
    monkeypatch.setattr(ep, "inspect_receipt", forbidden)
    assert not kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)


@pytest.mark.parametrize("change", ["pid", "start", "cgroup"])
def test_frozen_supervisor_identity_rejects_replacement(process_wait, change):
    conn, card, run, wait, props, output = process_wait
    assert kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
    write_result(output)
    finish(props)
    if change == "pid":
        props["ExecMainPID"] = "424243"
    elif change == "start":
        props["ExecMainStartTimestampMonotonic"] = str(int(props["ExecMainStartTimestampMonotonic"]) + 1)
    else:
        props["ControlGroup"] = "/test/other.service"
    assert kb.reconcile_external_waits(conn) == []
    assert "external_process_receipt" not in kb.get_run(conn, run).metadata


def test_live_proc_mismatch_rejects_admission(process_wait, monkeypatch):
    conn, card, run, wait, props, output = process_wait
    original = ep._proc_identity
    def wrong(pid):
        result = original(pid)
        if pid != os.getpid():
            result["start_ticks"] += 10 * os.sysconf("SC_CLK_TCK")
        return result
    monkeypatch.setattr(ep, "_proc_identity", wrong)
    assert not kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)


def test_corrupt_foreign_wait_never_reads_profile(process_wait, monkeypatch):
    conn, card, run, wait, props, output = process_wait
    assert kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
    metadata = kb.get_run(conn, run).metadata
    metadata["external_wait"]["executor"] = "default"
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps(metadata), run))
    conn.commit()
    def forbidden(*args, **kwargs):
        raise AssertionError("foreign inspection during reconciliation")
    monkeypatch.setattr(ep, "inspect_receipt", forbidden)
    assert kb.reconcile_external_waits(conn) == []


def test_native_control_worker_cannot_be_the_executor(process_wait):
    conn, card, run, wait, props, output = process_wait
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (424242, card))
    conn.commit()
    assert not kb.schedule_task(conn, card, reason="wait", expected_run_id=run, wait=wait)
