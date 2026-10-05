"""Real store/agent/tool/dispatcher rollover paths; provider calls are offline."""
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_context as wc


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    path = tmp_path / "board.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    with kb.connect_closing(path) as conn:
        tid = kb.create_task(conn, title="Continue safely", body="Approval: tests only; never publish.",
                             assignee="default", subscription_only=True,
                             provider_override="openai-codex", model_override="gpt-6-astra",
                             workspace_kind="dir", workspace_path=str(tmp_path))
        task = kb.claim_task(conn, tid, claimer="test-owner")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET consecutive_failures = 1 WHERE id = ?", (tid,))
            conn.execute("UPDATE task_runs SET metadata = ? WHERE id = ?",
                         (json.dumps({"candidate_refs": ["candidate1"], "artifacts": ["receipt.txt"]}),
                          task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", task.claim_lock)
    return path, task


@pytest.fixture
def agent(worker):
    from run_agent import AIAgent
    from hermes_state import SessionDB
    db = SessionDB(db_path=Path(os.environ["HERMES_HOME"]) / "state.db")
    with (patch("run_agent.get_tool_definitions", return_value=[]),
          patch("run_agent.check_toolset_requirements", return_value={}),
          patch("run_agent.OpenAI")):
        a = AIAgent(api_key="offline-fixture", base_url="https://example.invalid/v1",
                    model="offline-model", quiet_mode=True, skip_context_files=True,
                    skip_memory=True, session_db=db)
    a.subscription_only = True
    a.provider = "openai-codex"
    a.model = "gpt-6-astra"
    a.base_url = "https://chatgpt.com/backend-api/codex"
    a.api_mode = "chat_completions"
    a._cached_system_prompt = "Offline test. Never publish."
    a._use_prompt_caching = False
    a.save_trajectories = False
    a.client = MagicMock()
    a.client.chat.completions.create.side_effect = AssertionError("unexpected provider call")
    yield a
    a.close()
    db.close()


def compress(agent, messages=None):
    with pytest.raises(wc.ContextContinuation) as raised:
        agent._compress_context(messages or [
            {"role": "assistant", "content": "Phase completed. candidate1 retained."},
            {"role": "tool", "tool_call_id": "effect1", "content": "receipt.txt: succeeded"},
        ], "Never publish.", approx_tokens=209077)
    return raised.value.result


def test_preflight_parks_before_auxiliary_and_releases_turn_lease(worker, agent, monkeypatch):
    path, task = worker
    agent.compression_enabled = True
    agent.context_compressor.threshold_tokens = 1000
    agent.context_compressor.should_compress = lambda *args: True
    auxiliary = MagicMock(side_effect=AssertionError("auxiliary forbidden"))
    monkeypatch.setattr("agent.auxiliary_client.get_text_auxiliary_client", auxiliary)
    with patch("agent.conversation_compression.compress_context", auxiliary):
        result = agent.run_conversation("Continue", conversation_history=[
            {"role": "user", "content": "Approved tests only."},
            {"role": "assistant", "content": "receipt.txt completed " + "x" * 30000},
        ])
    assert result["context_parked"] and not result["failed"]
    auxiliary.assert_not_called()
    agent.client.chat.completions.create.assert_not_called()
    assert not getattr(agent, "_active_session_turn_lease_holder", None)
    with kb.connect_closing(path) as conn:
        parked = kb.get_task(conn, task.id)
        assert parked.status == "scheduled" and parked.current_run_id is None
        assert parked.consecutive_failures == 1
        events = [e for e in kb.list_events(conn, task.id) if e.kind == "context_evidence"]
        assert len(events) == 1
        assert "receipt.txt completed" not in json.dumps(events[0].payload)
        stored = wc.inspect_reference(conn, task.id, {"kind": "event", "id": events[0].id,
            "message_id": events[0].payload["last_message_id"] - 1})
        assert "receipt.txt completed" in stored["message"]["content"]
        run = kb.list_runs(conn, task.id)[-1]
        assert run.metadata["candidate_refs"] == ["candidate1"]
        assert run.metadata["artifacts"] == ["receipt.txt"]
    assert compress(agent)["failed"]  # same run cannot park twice


def test_output_cap_catch_unwinds_without_retry(worker, agent, monkeypatch):
    agent.compression_enabled = True
    agent.context_compressor.should_compress = lambda *args: False
    agent.context_compressor.context_length = 200000
    agent.max_tokens = 65536
    error = Exception("max_tokens: 65536 > context_window: 200000 - input_tokens: 199000 = available_tokens: 1000")
    error.status_code = 400
    error.code = 400
    agent.client.chat.completions.create.side_effect = error
    with patch.object(agent.context_compressor, "update_model"):
        result = agent.run_conversation("Continue")
    assert result["context_parked"] and not result["failed"]
    assert agent.client.chat.completions.create.call_count == 1


def test_non_worker_keeps_normal_policy_error(worker, agent, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    with pytest.raises(RuntimeError, match="subscription_only prohibits auxiliary"):
        agent._compress_context([], "system")


def test_failed_commit_does_not_leave_partial_evidence(worker, agent, monkeypatch):
    path, task = worker
    def reject_commit(*args, **kwargs):
        raise OSError("injected storage failure")
    monkeypatch.setattr(kb, "_end_run", reject_commit)
    assert compress(agent)["failed"]
    with kb.connect_closing(path) as conn:
        assert kb.get_task(conn, task.id).current_run_id == task.current_run_id
        assert not any(e.kind == "context_evidence" for e in kb.list_events(conn, task.id))
    # A board rollback must not erase the already-durable private transcript.
    assert any("receipt.txt: succeeded" in (row.get("content") or "")
               for row in agent._session_db.get_messages(agent.session_id))


def test_reopened_dispatcher_resumes_once_without_reset_or_replay(worker, agent, monkeypatch):
    path, task = worker
    assert compress(agent)["context_parked"]
    with kb.connect_closing(path) as conn:
        wc.resume_context_tasks(conn)
        assert kb.get_task(conn, task.id).status == "scheduled"  # owner still alive
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "normal")
    seen = []
    def spawn(fresh, workspace, **kwargs):
        seen.append(fresh)
        return None
    with kb.connect_closing(path) as conn:
        kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1)
        assert len(seen) == 1
        fresh = seen[0]
        assert fresh.current_run_id != task.current_run_id
        assert fresh.consecutive_failures == 1 and fresh.subscription_only
        context = json.loads(kb.build_worker_context(conn, task.id))
        assert context["reconciliation_required"]
        assert "receipt.txt: succeeded" not in json.dumps(context)
        assert context["continuation"]["receipt_refs"]
        ref = context["continuation"]
        receipt = wc.inspect_reference(conn, task.id, {
            "kind": "event", "id": ref["evidence_event_id"],
            "message_id": ref["receipt_refs"][-1]["message_id"]})
        assert receipt["message"]["content"] == "receipt.txt: succeeded"
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(fresh.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", fresh.claim_lock)
    assert compress(agent)["context_parked"]
    with kb.connect_closing(path) as conn:
        wc.resume_context_tasks(conn)
        assert kb.get_task(conn, task.id).status == "scheduled"
        assert kb.get_task(conn, task.id).consecutive_failures == 1


@pytest.mark.parametrize("violation", ["stale_run", "claim", "assignee", "child", "cron", "hold", "interrupt", "expired"])
def test_invalid_owners_fail_closed(worker, agent, monkeypatch, violation):
    from contextlib import nullcontext
    from agent.delegation_context import delegated_child_context, non_dispatcher_owned_context
    path, task = worker
    scope = nullcontext()
    if violation == "stale_run":
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id + 1))
    elif violation == "claim":
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "stale")
    elif violation == "assignee":
        monkeypatch.setenv("HERMES_PROFILE", "foreign")
    elif violation == "child":
        scope = delegated_child_context()
    elif violation == "cron":
        scope = non_dispatcher_owned_context()
    elif violation == "interrupt":
        agent._interrupt_requested = True
    elif violation == "hold":
        with kb.connect_closing(path) as conn, kb.write_txn(conn):
            kb._append_event(conn, task.id, "blocked", {"kind": "needs_input", "recurrences": 1})
    elif violation == "expired":
        with kb.connect_closing(path) as conn, kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (task.id,))
    with scope:
        assert compress(agent)["failed"]
    with kb.connect_closing(path) as conn:
        assert kb.get_task(conn, task.id).current_run_id == task.current_run_id
        assert not any(e.kind == "context_evidence" for e in kb.list_events(conn, task.id))


def test_bounded_show_keeps_checkpoint_and_references_restrictions(worker, monkeypatch):
    from tools.kanban_tools import _handle_show
    path, task = worker
    checkpoint = {"phase": "tests", "approval_refs": ["task body: tests only"],
                  "effect_refs": ["receipt.txt"], "candidate_refs": ["candidate1"],
                  "next": "Inspect receipt; do not repeat completed test"}
    with kb.connect_closing(path) as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET body = ? WHERE id = ?", ("Never publish. " * 10000, task.id))
        kb.add_comment(conn, task.id, "default", json.dumps({"phase_checkpoint": checkpoint}))
        for _ in range(24):
            kb.add_comment(conn, task.id, "default", "large history " * 2000)
        for _ in range(12):
            parent = kb.create_task(conn, title="parent", body="p" * 30000)
            kb.complete_task(conn, parent, result="handoff " * 10000)
            kb.link_tasks(conn, parent, task.id)
    output = _handle_show({})
    assert len(output.encode()) <= wc.BOOTSTRAP_LIMIT
    compact = json.loads(output)
    assert compact["checkpoint"]["value"] == checkpoint
    assert compact["required_restriction_refs"]
    assert "worker_context" not in compact and "runs" not in compact
    body = json.loads(_handle_show({"reference": {"kind": "task", "id": task.id, "field": "body"}}))
    assert body["body"] == "Never publish. " * 10000
    full = json.loads(_handle_show({"full": True}))
    assert len(full["comments"]) == 25


def test_progress_requires_new_phase_and_effects_and_has_hard_ceiling(worker, agent, monkeypatch):
    path, task = worker
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    for phase in range(wc.MAX_STEP_CONTINUATIONS + 1):
        with kb.connect_closing(path) as conn:
            kb.add_comment(conn, task.id, "default", json.dumps({"phase_checkpoint": {
                "phase": str(phase), "approval_refs": ["task body"],
                "effect_refs": [f"receipt-{phase}"], "next": "reconcile then continue"}}))
        assert compress(agent)["context_parked"]
        with kb.connect_closing(path) as conn:
            wc.resume_context_tasks(conn)
            current = kb.get_task(conn, task.id)
            if phase == wc.MAX_STEP_CONTINUATIONS:
                assert current.status == "scheduled"
                assert current.consecutive_failures == 1
            else:
                assert current.status == "ready"
                current = kb.claim_task(conn, task.id)
                monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(current.current_run_id))
                monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", current.claim_lock)


@pytest.mark.parametrize("barrier", ["estop", "hold", "parent", "assignee", "policy"])
def test_resume_respects_current_controls(worker, agent, monkeypatch, barrier):
    path, task = worker
    assert compress(agent)["context_parked"]
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kb.connect_closing(path) as conn:
        if barrier == "estop":
            (Path(os.environ["HERMES_HOME"]) / "ESTOP").touch()
        elif barrier == "hold":
            with kb.write_txn(conn):
                kb._append_event(conn, task.id, "blocked", {"kind": "needs_input", "recurrences": 1})
        elif barrier == "parent":
            parent = kb.create_task(conn, title="unfinished parent")
            kb.link_tasks(conn, parent, task.id)
        else:
            with kb.write_txn(conn):
                if barrier == "policy":
                    conn.execute("UPDATE tasks SET subscription_only = 0 WHERE id = ?", (task.id,))
                else:
                    conn.execute("UPDATE tasks SET assignee = 'different' WHERE id = ?", (task.id,))
        wc.resume_context_tasks(conn)
        assert kb.get_task(conn, task.id).status == "scheduled"


def test_review_restriction_inspection_does_not_replay_bootstrap(worker, agent):
    path, task = worker
    bootstrap = "Already installed system bootstrap. " * 10000
    agent._cached_system_prompt = bootstrap
    instruction = "Approval revoked; do not publish."
    assert compress(agent, [
        {"role": "system", "content": bootstrap},
        {"role": "user", "content": instruction},
    ])["context_parked"]
    with kb.connect_closing(path) as conn:
        event = next(e for e in kb.list_events(conn, task.id) if e.kind == "context_evidence")
        restrictions = wc.inspect_reference(conn, task.id, {
            "kind": "event", "id": event.id, "field": "restrictions"})
        assert len(json.dumps(restrictions).encode()) <= wc.BOOTSTRAP_LIMIT
        assert bootstrap not in json.dumps(restrictions)
        assert instruction in json.dumps(restrictions)
        assert wc.inspect_reference(conn, task.id,
            restrictions["system_prompt_reference"])["system_message"] == bootstrap


def test_review_private_context_never_enters_shared_show(worker, agent, monkeypatch):
    from tools.kanban_tools import _handle_show
    path, task = worker
    secret = "PROFILE_PRIVATE_TOOL_RECEIPT_" + "z" * 40000
    private_system = "PROFILE_PRIVATE_SYSTEM_INSTRUCTION"
    agent._cached_system_prompt = private_system
    assert compress(agent, [{"role": "tool", "content": secret}])["context_parked"]
    agent.close()
    with kb.connect_closing(path) as conn:
        events = kb.list_events(conn, task.id)
        assert secret not in json.dumps([e.payload for e in events])
        assert private_system not in json.dumps([e.payload for e in events])
        evidence = next(e for e in events if e.kind == "context_evidence")
        assert wc.inspect_reference(conn, task.id, {"kind": "event", "id": evidence.id,
                                                    "message_id": evidence.payload["last_message_id"]})["message"]["content"] == secret
        assert wc.inspect_reference(conn, task.id, {"kind": "event", "id": evidence.id,
                                                    "field": "system_prompt"})["system_message"] == private_system
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.setenv("HERMES_PROFILE", "foreign")
    for full in (False, True):
        shared = _handle_show({"task_id": task.id, "full": full})
        assert secret not in shared and private_system not in shared


@pytest.mark.parametrize("identity", ["foreign_profile", "foreign_home", "spoofed_home"])
def test_review_receipt_inspection_requires_saved_owner_home(worker, agent, monkeypatch, tmp_path, identity):
    path, task = worker
    assert compress(agent)["context_parked"]
    with kb.connect_closing(path) as conn:
        event = next(e for e in kb.list_events(conn, task.id) if e.kind == "context_evidence")
        if identity == "foreign_profile":
            monkeypatch.setenv("HERMES_PROFILE", "foreign")
        elif identity == "foreign_home":
            monkeypatch.setenv("HERMES_HOME", str(tmp_path / "foreign"))
        else:
            # Matching the saved strings alone is insufficient: canonical
            # profile resolution must also match the currently served home.
            monkeypatch.setattr("hermes_cli.profiles.profile_matches_home", lambda *a, **k: False)
        with pytest.raises(ValueError, match="owner|profile|home"):
            wc.inspect_reference(conn, task.id, {"kind": "event", "id": event.id,
                                                "message_id": event.payload["last_message_id"]})


def _all_comment_references(conn, tid, context):
    found = {c["id"] for c in context.get("comments", [])}
    pending = list(context["required_restriction_refs"])
    while pending:
        ref = pending.pop()
        if ref["kind"] == "comment":
            found.add(ref["id"])
        elif ref["kind"] == "comments":
            page = wc.inspect_reference(conn, tid, ref)
            pending.extend(page["references"])
            if page.get("next_reference"):
                pending.append(page["next_reference"])
    return found


def test_review_revocation_cannot_be_hidden_by_five_later_comments(worker, agent, monkeypatch):
    path, task = worker
    assert compress(agent)["context_parked"]
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    with kb.connect_closing(path) as conn:
        revoked = kb.add_comment(conn, task.id, "owner", "Approval revoked; do not publish X.")
        for _ in range(5):
            kb.add_comment(conn, task.id, "worker", "routine chatter")
        wc.resume_context_tasks(conn)
        assert kb.claim_task(conn, task.id)
        context = json.loads(wc.worker_context(conn, task.id))
        assert revoked in _all_comment_references(conn, task.id, context)
        assert context["reconciliation_required"]
        assert wc.inspect_reference(conn, task.id, {"kind": "comment", "id": revoked})["body"].startswith("Approval revoked")


def test_review_all_initial_comment_refs_fit_total_budget(worker):
    path, task = worker
    with kb.connect_closing(path) as conn:
        ids = {kb.add_comment(conn, task.id, "owner", "Instruction " * 5000) for _ in range(220)}
        context = wc.worker_context(conn, task.id)
        assert len(context.encode()) <= wc.BOOTSTRAP_LIMIT
        assert ids <= _all_comment_references(conn, task.id, json.loads(context))


def test_review_subscription_finalizer_never_enters_micro_summarizer(worker, agent, monkeypatch):
    from agent.turn_finalizer import finalize_turn
    compressor = agent.context_compressor
    compressor._micro_compact_enabled = True
    compressor.protect_first_n = 1
    compressor.protect_last_n = 2
    summarize = MagicMock(return_value="should not summarize")
    monkeypatch.setattr(compressor, "_micro_summarize_one", summarize)
    messages = [{"role": "system", "content": "private system"}]
    for index in range(6):
        messages += [{"role": "user", "content": f"question {index}"},
                     {"role": "assistant", "content": "answer " + "z" * 400}]
    result = finalize_turn(agent, final_response="complete", api_call_count=1,
        interrupted=False, failed=False, messages=messages, conversation_history=None,
        effective_task_id="review-test", turn_id="review-turn", user_message="question",
        original_user_message="question", _should_review_memory=False, _turn_exit_reason="unknown")
    summarize.assert_not_called()
    assert result["final_response"] == "complete"
    assert any(m.get("content") == "question 0" for m in messages)


def test_review_non_json_source_and_session_write_failure_do_not_park(worker, agent, monkeypatch):
    path, task = worker
    assert compress(agent, [{"role": "tool", "content": object()}])["failed"]
    with patch.object(agent._session_db, "append_messages_batch", side_effect=OSError("disk failure")):
        assert compress(agent)["failed"]
    with kb.connect_closing(path) as conn:
        assert kb.get_task(conn, task.id).current_run_id == task.current_run_id
        assert not any(e.kind == "context_evidence" for e in kb.list_events(conn, task.id))


def test_review_native_message_tamper_fails_digest_check(worker, agent):
    path, task = worker
    assert compress(agent)["context_parked"]
    with kb.connect_closing(path) as conn:
        event = next(e for e in kb.list_events(conn, task.id) if e.kind == "context_evidence")
        # Exercise read-back integrity through the native session API.
        agent._session_db.update_system_prompt(agent.session_id, "changed after checkpoint")
        with pytest.raises(ValueError, match="changed|incomplete"):
            wc.inspect_reference(conn, task.id, {"kind": "event", "id": event.id,
                                                "field": "restrictions"})
