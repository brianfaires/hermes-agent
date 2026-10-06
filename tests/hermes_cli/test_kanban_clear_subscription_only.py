"""Controller-only legacy flag retirement, using isolated real board APIs."""
import argparse
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path

import pytest

from agent.delegation_context import delegated_child_context, non_dispatcher_owned_context
from hermes_cli import kanban as cli, kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    kb.init_db()
    with kb.connect_closing() as conn:
        yield conn


def invoke(*argv):
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    return cli.kanban_command(parser.parse_args(["kanban", *argv]))


def clear(conn, tid):
    return kb.clear_subscription_only(conn, tid, actor="default", reason="Brian retired restriction")


@pytest.mark.parametrize("state", ["todo", "blocked", "archived"])
def test_clear_preserves_task_and_history_and_is_idempotent(board, state):
    conn = board
    parent = kb.create_task(conn, title="parent", subscription_only=True,
                            provider_override="openai-codex", model_override="gpt-5.3-codex")
    tid = kb.create_task(conn, title="legacy", parents=[] if state == "blocked" else [parent], subscription_only=True,
                         model_override="gpt-5.3-codex", provider_override="openai-codex",
                         reasoning_effort="medium", priority=7)
    kb.add_comment(conn, tid, "default", "Existing history")
    if state == "blocked":
        assert kb.block_task(conn, tid, reason="operator hold", kind="needs_input")
        kb.link_tasks(conn, parent, tid)
    elif state == "archived":
        kb.archive_task(conn, tid)
    assert kb.get_task(conn, tid).status == state
    before = asdict(kb.get_task(conn, tid))
    parent_before = kb.get_task(conn, parent)
    events = kb.list_events(conn, tid)
    comments = kb.list_comments(conn, tid)
    runs = kb.list_runs(conn, tid)
    held = kb.has_active_control_hold(conn, tid)
    if state == "blocked":
        assert held
    graph = kb.task_graph_context(conn, tid)
    for previous in (True, False):
        assert clear(conn, tid)
        assert asdict(kb.get_task(conn, tid)) == {**before, "subscription_only": False}
        assert kb.get_task(conn, parent) == parent_before
        assert kb.list_comments(conn, tid) == comments
        assert kb.list_runs(conn, tid) == runs
        assert kb.has_active_control_hold(conn, tid) == held
        assert kb.task_graph_context(conn, tid) == graph
        after = kb.list_events(conn, tid)
        assert after[:-1] == events
        assert after[-1].kind == "subscription_only_cleared"
        assert after[-1].payload == {
            "actor": "default", "reason": "Brian retired restriction",
            "previous": previous, "subscription_only": False, "changed": previous,
        }
        events = after


@pytest.mark.parametrize("context", ["same", "cross", "child", "child_env", "cron"])
def test_workers_rejected_by_db_and_cli(board, monkeypatch, context):
    tid = kb.create_task(board, title="legacy", subscription_only=True,
                         provider_override="openai-codex", model_override="gpt-5.3-codex")
    before, events = kb.get_task(board, tid), kb.list_events(board, tid)
    scope = nullcontext()
    if context in {"same", "cross"}:
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid if context == "same" else "t_other")
    elif context == "child":
        scope = delegated_child_context()
    elif context == "child_env":
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    else:
        scope = non_dispatcher_owned_context()
    with scope:
        with pytest.raises(PermissionError):
            clear(board, tid)
        assert invoke("clear-subscription-only", tid, "--reason", "authorized") != 0
    assert kb.get_task(board, tid) == before
    assert kb.list_events(board, tid) == events


def test_cli_and_missing_task(board, capsys):
    tid = kb.create_task(board, title="legacy", subscription_only=True,
                         provider_override="openai-codex", model_override="gpt-5.3-codex")
    assert invoke("clear-subscription-only", tid, "--reason", "retired") == 0
    assert not kb.get_task(board, tid).subscription_only
    assert clear(board, "t_missing") is False
    assert invoke("clear-subscription-only", "t_missing", "--reason", "retired") == 1
    assert "no such task" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        invoke("clear-subscription-only", "--reason", "retired")


def test_event_failure_rolls_back_flag(board, monkeypatch):
    tid = kb.create_task(board, title="legacy", subscription_only=True,
                         provider_override="openai-codex", model_override="gpt-5.3-codex")
    events = kb.list_events(board, tid)
    def fail(*args, **kwargs):
        raise RuntimeError("audit failure")
    monkeypatch.setattr(kb, "_append_event", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        clear(board, tid)
    assert kb.get_task(board, tid).subscription_only
    assert kb.list_events(board, tid) == events


def test_clear_preserves_active_claim_and_run(board):
    tid = kb.create_task(board, title="active", subscription_only=True,
                         provider_override="openai-codex", model_override="gpt-5.3-codex")
    assert kb.claim_task(board, tid, claimer="test-controller")
    before = asdict(kb.get_task(board, tid))
    runs = kb.list_runs(board, tid)
    assert before["claim_lock"] and runs
    assert clear(board, tid)
    assert asdict(kb.get_task(board, tid)) == {**before, "subscription_only": False}
    assert kb.list_runs(board, tid) == runs


@pytest.mark.parametrize("field", ["actor", "reason"])
def test_empty_audit_provenance_rejected(board, field):
    tid = kb.create_task(board, title="task")
    events = kb.list_events(board, tid)
    kwargs = {"actor": "default", "reason": "retired", field: "   "}
    with pytest.raises(ValueError, match=field):
        kb.clear_subscription_only(board, tid, **kwargs)
    assert kb.list_events(board, tid) == events
