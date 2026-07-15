from __future__ import annotations

from pathlib import Path

from gateway.config import GatewayConfig, Platform
from gateway.kanban_mirror.context import resolve_mirrored_kanban_thread
from gateway.kanban_mirror.state import add_member, connect_mirror, create_initiative, mirror_db_path, set_thread
from gateway.session import SessionSource, build_session_context, build_session_context_prompt
from gateway.session_context import clear_session_vars, set_session_vars
from hermes_cli import kanban_db as kb
from tools import kanban_tools


def _make_board(monkeypatch, tmp_path: Path, board: str = "operations"):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    kb.create_board(board)
    conn = kb.connect(board=board)
    return conn


def _mirror_thread(board: str, *, kind: str, thread_id: str, members: list[str]) -> None:
    conn = connect_mirror(mirror_db_path(board))
    try:
        create_initiative(conn, f"init_{thread_id}", f"Thread {thread_id}", kind=kind)
        set_thread(conn, f"init_{thread_id}", thread_id, thread_id)
        for task_id in members:
            add_member(conn, f"init_{thread_id}", task_id)
    finally:
        conn.close()


def test_single_card_thread_resolves_safe_default_and_prompt(monkeypatch, tmp_path):
    conn = _make_board(monkeypatch, tmp_path)
    try:
        task_id = kb.create_task(
            conn,
            title="Parent card",
            body="Root work",
            assignee="ops",
            priority=7,
            initial_status="blocked",
            board="operations",
        )
    finally:
        conn.close()
    _mirror_thread("operations", kind="post", thread_id="thread-1", members=[task_id])

    ctx = resolve_mirrored_kanban_thread("thread-1")

    assert ctx is not None
    assert ctx.board_slug == "operations"
    assert ctx.safe_default_task_id == task_id
    assert ctx.is_multi_card is False
    assert ctx.tasks[task_id].title == "Parent card"

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        parent_chat_id="forum-1",
        thread_id="thread-1",
        chat_name="Parent thread",
        chat_type="thread",
    )
    session_context = build_session_context(source, GatewayConfig())
    prompt = build_session_context_prompt(session_context)
    assert "Linked Kanban mirror" in prompt
    assert f"Primary task: `{task_id}`" in prompt
    assert "Default Kanban tools target this task" in prompt


def test_digest_thread_is_multi_card_no_safe_default(monkeypatch, tmp_path):
    conn = _make_board(monkeypatch, tmp_path)
    try:
        first = kb.create_task(conn, title="First", assignee="ops", board="operations")
        second = kb.create_task(conn, title="Second", assignee="ops", board="operations")
    finally:
        conn.close()
    _mirror_thread("operations", kind="digest", thread_id="digest-thread", members=[first, second])

    ctx = resolve_mirrored_kanban_thread("digest-thread")

    assert ctx is not None
    assert ctx.safe_default_task_id is None
    assert ctx.is_multi_card is True
    assert ctx.task_ids == [first, second]

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="digest-thread",
        parent_chat_id="forum-1",
        thread_id="digest-thread",
        chat_type="thread",
    )
    prompt = build_session_context_prompt(build_session_context(source, GatewayConfig()))
    assert "Multi-card thread" in prompt
    assert "do not choose a Kanban task silently" in prompt


def test_parent_thread_with_child_rollup_still_defaults_parent(monkeypatch, tmp_path):
    conn = _make_board(monkeypatch, tmp_path)
    try:
        parent = kb.create_task(conn, title="Parent", assignee="ops", board="operations")
        child = kb.create_task(conn, title="Child", assignee="ops", parents=[parent], board="operations")
    finally:
        conn.close()
    _mirror_thread("operations", kind="post", thread_id="parent-thread", members=[parent])

    ctx = resolve_mirrored_kanban_thread("parent-thread")

    assert ctx is not None
    assert ctx.safe_default_task_id == parent
    assert child not in ctx.task_ids
    assert ctx.is_multi_card is False


def test_kanban_tools_use_session_context_default_task_and_board(monkeypatch, tmp_path):
    conn = _make_board(monkeypatch, tmp_path)
    try:
        task_id = kb.create_task(conn, title="Context scoped", assignee="ops", board="operations")
    finally:
        conn.close()
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    tokens = set_session_vars(kanban_board="operations", kanban_task=task_id)
    try:
        assert kanban_tools._default_task_id(None) == task_id
        _kb, scoped_conn = kanban_tools._connect()
        try:
            assert _kb.get_task(scoped_conn, task_id).title == "Context scoped"
        finally:
            scoped_conn.close()
    finally:
        clear_session_vars(tokens)
