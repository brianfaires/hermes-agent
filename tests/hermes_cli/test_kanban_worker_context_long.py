"""Opt-in real subprocess duration check; no provider or auxiliary inference.

Run ONLY when explicitly requested:
  HERMES_PYTHON=<approved-python> scripts/run_tests.sh \
    tests/hermes_cli/test_kanban_worker_context_long.py \
    -k real_subprocess_305_seconds --file-timeout 420 --file-retries 0

This is a local integration test, not installed/live acceptance evidence.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb


_AGENT_SETUP = '''
import json, os, time
from pathlib import Path
from unittest.mock import MagicMock, patch
from hermes_cli import kanban_db as kb
from run_agent import AIAgent
from hermes_state import SessionDB
with (patch("run_agent.get_tool_definitions", return_value=[]),
      patch("run_agent.check_toolset_requirements", return_value={}),
      patch("run_agent.OpenAI")):
    agent = AIAgent(api_key="offline-fixture", base_url="https://example.invalid/v1",
                    model="offline-model", quiet_mode=True, skip_context_files=True,
                    skip_memory=True, session_db=SessionDB())
agent.subscription_only = True
agent.provider = "openai-codex"
agent.model = "gpt-6-astra"
agent.base_url = "https://chatgpt.com/backend-api/codex"
agent.api_mode = "chat_completions"
agent._cached_system_prompt = "Only reconcile the existing test; never restart it. " * 6000
agent._use_prompt_caching = False
agent.save_trajectories = False
agent.client = MagicMock()
agent.client.chat.completions.create.side_effect = AssertionError("inference forbidden")
root = Path(os.environ["ROOK_TEST_ROOT"])
tid = os.environ["HERMES_KANBAN_TASK"]
'''


@pytest.mark.timeout(420)
def test_real_subprocess_305_seconds(tmp_path, monkeypatch, request):
    if request.config.option.keyword != "real_subprocess_305_seconds":
        pytest.skip("opt in with -k real_subprocess_305_seconds (305 real seconds)")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    path = tmp_path / "board.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "normal")
    with kb.connect_closing(path) as conn:
        tid = kb.create_task(conn, title="Preserve required long test", assignee="default",
                            body="Tests only. Reconcile required-receipt.json; do not restart the test.",
                            subscription_only=True, provider_override="openai-codex",
                            model_override="gpt-6-astra", workspace_kind="dir", workspace_path=str(tmp_path))
        owner = kb.claim_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET consecutive_failures = 1 WHERE id = ?", (tid,))
        kb.add_comment(conn, tid, "default", json.dumps({"phase_checkpoint": {
            "phase": "required test running", "approval_refs": ["task body: tests only"],
            "effect_refs": [str(tmp_path / "required-receipt.json")],
            "candidate_refs": ["candidate1"], "next": "Wait for the existing process receipt; do not replay"}}))
    env = {**os.environ, "HOME": str(tmp_path), "ROOK_TEST_ROOT": str(tmp_path),
           "HERMES_KANBAN_TASK": tid, "HERMES_KANBAN_RUN_ID": str(owner.current_run_id),
           "HERMES_KANBAN_CLAIM_LOCK": owner.claim_lock}
    required_code = '''
import json, os, time
from pathlib import Path
root = Path(os.environ["ROOK_TEST_ROOT"])
with (root / "executions").open("a") as stream:
    stream.write("started\\n")
start = time.monotonic()
time.sleep(305)
elapsed = time.monotonic() - start
assert elapsed > 300
temporary = root / "required-receipt.tmp"
temporary.write_text(json.dumps({"elapsed": elapsed, "passed": True, "pid": os.getpid()}))
temporary.replace(root / "required-receipt.json")
'''
    required = subprocess.Popen([sys.executable, "-c", required_code], env=env, start_new_session=True)
    fresh = None
    log = (tmp_path / "workers.log").open("w")
    start = time.monotonic()
    try:
        first_code = _AGENT_SETUP + '''
agent.compression_enabled = True
agent.context_compressor.threshold_tokens = 1000
agent.context_compressor.should_compress = lambda *args: True
result = agent.run_conversation("Continue the required test", conversation_history=[
    {"role": "user", "content": "Do not restart the test process."},
    {"role": "assistant", "content": "Required test running: " + str(root / "required-receipt.json") + " x" * 15000}])
assert result["context_parked"] and not result["failed"]
agent.client.chat.completions.create.assert_not_called()
(root / "old-session").write_text(agent.session_id)
agent.close()
'''
        first = subprocess.run([sys.executable, "-c", first_code], env=env,
                               stdout=log, stderr=log, timeout=90)
        assert first.returncode == 0, (tmp_path / "workers.log").read_text()[-4000:]
        assert required.poll() is None
        fresh_code = _AGENT_SETUP + '''
from tools.kanban_tools import _handle_show
context = json.loads(_handle_show({}))
assert context["reconciliation_required"]
assert context["checkpoint"]["value"]["candidate_refs"] == ["candidate1"]
for reference in context["required_restriction_refs"]:
    if reference.get("kind") == "event" and reference.get("field") == "restrictions":
        restrictions = json.loads(_handle_show({"reference": reference}))
        assert len(json.dumps(restrictions).encode()) < 24000
        assert "system_message" not in restrictions
        assert "Do not restart the test process." in json.dumps(restrictions)
        assert restrictions["system_prompt_reference"]["field"] == "system_prompt"
assert agent.session_id != (root / "old-session").read_text()
assert " x x x " not in json.dumps(context)
(root / "fresh-session").write_text(agent.session_id)
deadline = time.monotonic() + 360
receipt = root / "required-receipt.json"
while not receipt.exists():
    assert time.monotonic() < deadline
    time.sleep(0.2)
result = json.loads(receipt.read_text())
assert result["passed"] and result["elapsed"] > 300
assert (root / "executions").read_text().splitlines() == ["started"]
with kb.connect_closing() as conn:
    assert kb.complete_task(conn, tid, result="Required test passed; existing receipt reconciled.",
        metadata={"candidate_refs": ["candidate1"], "artifacts": [str(receipt)]},
        expected_run_id=int(os.environ["HERMES_KANBAN_RUN_ID"]))
agent.client.chat.completions.create.assert_not_called()
agent.close()
'''
        def spawn(task, workspace, **kwargs):
            nonlocal fresh
            fresh_env = {**env, "HERMES_KANBAN_RUN_ID": str(task.current_run_id),
                         "HERMES_KANBAN_CLAIM_LOCK": task.claim_lock}
            fresh = subprocess.Popen([sys.executable, "-c", fresh_code], env=fresh_env,
                                     stdout=log, stderr=log)
            return fresh.pid
        with kb.connect_closing(path) as conn:
            kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1)
            resumed = kb.get_task(conn, tid)
            assert resumed.current_run_id != owner.current_run_id
            assert resumed.consecutive_failures == 1
        assert fresh is not None
        assert fresh.wait(timeout=360) == 0, (tmp_path / "workers.log").read_text()[-4000:]
        assert required.wait(timeout=10) == 0
        assert time.monotonic() - start > 300
        with kb.connect_closing(path) as conn:
            assert kb.get_task(conn, tid).status == "done"
            runs = kb.list_runs(conn, tid)
            assert len(runs) == 2 and runs[0].outcome == "context_parked"
            assert runs[1].metadata["candidate_refs"] == ["candidate1"]
            assert len([e for e in kb.list_events(conn, tid) if e.kind == "context_evidence"]) == 1
    finally:
        for proc in (fresh, required):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        log.close()
