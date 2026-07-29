from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from plugins.platforms.discord.kanban_mirror.inbox import (
    DiscordReactionContext,
    KanbanReplyInboxConfig,
    _apply_cancel_thread_work,
    handle_reaction,
)
from plugins.platforms.discord.kanban_mirror.state import (
    add_member,
    backfill_legacy_bindings,
    connect_mirror,
    create_initiative,
    is_thread_quarantined,
    load_board_snapshot,
    mirror_db_path,
    prepare_binding_transition,
    receipt_exists,
    set_thread,
)

FORUM_ID = "1001"
THREAD_ID = "2002"
MESSAGE_ID = "4004"


def _config() -> KanbanReplyInboxConfig:
    return KanbanReplyInboxConfig(
        enabled=True,
        forum_channel_ids=frozenset({FORUM_ID}),
        allow_commands=frozenset({"comment", "block", "unblock"}),
        ack=True,
    )


def _reaction() -> DiscordReactionContext:
    return DiscordReactionContext(
        reaction_key=f"reaction:{THREAD_ID}:{MESSAGE_ID}:42:🗑",
        message_id=MESSAGE_ID,
        author_id="42",
        author_label="Brian",
        forum_channel_id=FORUM_ID,
        thread_id=THREAD_ID,
        emoji="🗑️",
        intent="cancel_thread_work",
        meaning="Cancel all remaining work listed on this thread.",
    )


@pytest.fixture
def ghost_topology(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    conn = kb.connect(db_path)
    try:
        ids = {
            name: kb.create_task(
                conn,
                title=title,
                body="body",
                assignee="ops",
                created_by="test",
                triage=True,
            )
            for name, title in (
                ("bound", "Completed bound stage"),
                ("root", "Root continuation"),
                ("shared", "Unrelated shared prerequisite"),
            )
        }
        generated = kb.decompose_triage_task(
            conn,
            ids["root"],
            root_assignee="ops",
            children=[
                {"title": "Generated A", "body": "body", "assignee": "ops"},
                {
                    "title": "Generated B",
                    "body": "body",
                    "assignee": "ops",
                    "parents": [0],
                },
            ],
            author="test",
            auto_promote=False,
        )
        assert generated is not None
        ids.update(generated_a=generated[0], generated_b=generated[1])
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (ids["bound"],))
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (ids["shared"],))
        conn.execute(
            "INSERT INTO task_links(parent_id,child_id) VALUES (?,?)",
            (ids["bound"], ids["root"]),
        )
        conn.execute(
            "INSERT INTO task_links(parent_id,child_id) VALUES (?,?)",
            (ids["shared"], ids["root"]),
        )
        conn.commit()
    finally:
        conn.close()

    mirror_conn = connect_mirror(mirror_db_path("default"))
    try:
        create_initiative(mirror_conn, "init_ghost", "Ghost topology")
        add_member(mirror_conn, "init_ghost", ids["bound"])
        set_thread(mirror_conn, "init_ghost", THREAD_ID, "9999")
        assert backfill_legacy_bindings(mirror_conn, "default") == 1
        prepare_binding_transition(
            mirror_conn,
            transition_key="test:bound-to-root",
            thread_id=THREAD_ID,
            old_card_metadata={"board_slug": "default", "task_id": ids["bound"]},
            new_card_metadata={"board_slug": "default", "task_id": ids["root"]},
            transition_payload={"kind": "continuation"},
            starter_payload={"title": "Root continuation"},
        )
    finally:
        mirror_conn.close()
    return db_path, ids


def _statuses(db_path, ids):
    conn = kb.connect(db_path)
    try:
        statuses = {}
        for name, task_id in ids.items():
            task = kb.get_task(conn, task_id)
            assert task is not None
            statuses[name] = task.status
        return statuses
    finally:
        conn.close()


def test_trash_archives_all_idle_owned_work_but_not_shared_prerequisite(ghost_topology):
    db_path, ids = ghost_topology
    result = handle_reaction(_reaction(), config=_config())
    assert result.reason == "handled"
    assert result.action == "reaction:cancel_thread_work"
    assert result.ack == "Cancelled 3 remaining Kanban card(s) on this thread."
    assert _statuses(db_path, ids) == {
        "bound": "done",
        "root": "archived",
        "shared": "blocked",
        "generated_a": "archived",
        "generated_b": "archived",
    }
    conn = kb.connect(db_path)
    try:
        assert kb.list_owner_instructions(conn, task_id=ids["bound"]) == []
        comments = kb.list_comments(conn, ids["bound"])
        assert len(comments) == 1
        assert ids["shared"] not in comments[0].body
    finally:
        conn.close()


def test_trash_on_stale_quarantined_thread_cancels_and_closes_the_repair_latch(
    ghost_topology,
):
    db_path, ids = ghost_topology
    mirror_conn = connect_mirror(mirror_db_path("default"))
    try:
        mirror_conn.execute(
            """INSERT INTO mirror_reconciliation_findings
               (finding_key,severity,code,thread_id,binding_key,task_id,evidence,
                evidence_hash,first_seen_at,last_seen_at,resolved_at)
               VALUES ('stale-successor','error','successor.selection_ambiguous',
                       ?,?,?,'{}','hash',10,10,NULL)""",
            (THREAD_ID, f"binding:{THREAD_ID}:1", ids["bound"]),
        )
        mirror_conn.execute(
            """INSERT INTO mirror_thread_quarantine
               (thread_id,needs_repair,quarantined_at,updated_at,resolved_at)
               VALUES (?,1,10,10,NULL)""",
            (THREAD_ID,),
        )
        mirror_conn.commit()
    finally:
        mirror_conn.close()

    result = handle_reaction(_reaction(), config=_config())

    assert result.reason == "handled"
    assert _statuses(db_path, ids) == {
        "bound": "done",
        "root": "archived",
        "shared": "blocked",
        "generated_a": "archived",
        "generated_b": "archived",
    }
    mirror_conn = connect_mirror(mirror_db_path("default"))
    try:
        assert not is_thread_quarantined(mirror_conn, THREAD_ID)
        assert mirror_conn.execute(
            "SELECT resolved_at FROM mirror_reconciliation_findings "
            "WHERE finding_key='stale-successor'"
        ).fetchone()[0] is not None
        assert mirror_conn.execute(
            "SELECT COUNT(*) FROM mirror_binding_transitions "
            "WHERE thread_id=? AND state='prepared'",
            (THREAD_ID,),
        ).fetchone()[0] == 0
    finally:
        mirror_conn.close()


def test_trash_retry_is_idempotent(ghost_topology):
    db_path, ids = ghost_topology
    assert handle_reaction(_reaction(), config=_config()).reason == "handled"
    assert handle_reaction(_reaction(), config=_config()).reason == "duplicate"
    conn = kb.connect(db_path)
    try:
        assert len(kb.list_comments(conn, ids["bound"])) == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='cancel_thread_work'"
        ).fetchone()[0] == 4
    finally:
        conn.close()


def test_crash_after_kanban_commit_before_receipt_is_idempotent(ghost_topology):
    db_path, ids = ghost_topology
    ctx = _reaction()
    conn = kb.connect(db_path)
    mirror_conn = connect_mirror(mirror_db_path("default"))
    try:
        assert _apply_cancel_thread_work(
            conn,
            mirror_conn,
            represented_task_id=ids["bound"],
            board_slug="default",
            ctx=ctx,
            source_key=ctx.reaction_key,
            action_prefix="reaction",
        ).reason == "handled"
    finally:
        mirror_conn.close()
        conn.close()
    assert handle_reaction(ctx, config=_config()).reason == "handled"
    conn = kb.connect(db_path)
    try:
        assert len(kb.list_comments(conn, ids["bound"])) == 1
    finally:
        conn.close()
    mirror_conn = connect_mirror(mirror_db_path("default"))
    try:
        assert receipt_exists(mirror_conn, ctx.reaction_key)
    finally:
        mirror_conn.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [("status", "running"), ("worker_pid", 424242)],
)
def test_trash_fails_closed_on_active_work(ghost_topology, field, value):
    db_path, ids = ghost_topology
    conn = kb.connect(db_path)
    try:
        conn.execute(
            f"UPDATE tasks SET {field}=? WHERE id=?",
            (value, ids["generated_a"]),
        )
        conn.commit()
    finally:
        conn.close()
    before = _statuses(db_path, ids)
    result = handle_reaction(_reaction(), config=_config())
    assert result.reason == "unsafe_active_work"
    assert _statuses(db_path, ids) == before


def test_trash_fails_closed_on_queued_owner_instruction(ghost_topology):
    db_path, ids = ghost_topology
    conn = kb.connect(db_path)
    try:
        conn.execute(
            "UPDATE tasks SET status='running' WHERE id=?",
            (ids["generated_a"],),
        )
        instruction = kb.create_owner_instruction(
            conn,
            task_id=ids["generated_a"],
            assignee="ops",
            source="test",
            source_key="queued-before-thread-cancel",
            actor="Brian",
            body="Instruction: rerun_request",
        )
        assert kb.route_owner_instruction(conn, instruction.id) == "queued"
        conn.execute(
            "UPDATE tasks SET status='blocked' WHERE id=?",
            (ids["generated_a"],),
        )
        conn.commit()
    finally:
        conn.close()
    before = _statuses(db_path, ids)

    result = handle_reaction(_reaction(), config=_config())

    assert result.reason == "pending_owner_instructions"
    assert _statuses(db_path, ids) == before
    conn = kb.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='cancel_thread_work'"
        ).fetchone()[0] == 0
        queued = kb.get_owner_instruction(conn, instruction.id)
        assert queued is not None
        assert queued.status == "queued"
    finally:
        conn.close()


def test_trash_fails_closed_on_unroutable_owner_instruction(ghost_topology):
    db_path, ids = ghost_topology
    conn = kb.connect(db_path)
    try:
        instruction = kb.create_owner_instruction(
            conn,
            task_id=ids["generated_a"],
            assignee="ops",
            source="test",
            source_key="unroutable-before-thread-cancel",
            actor="Brian",
            body="Instruction: rerun_request",
        )
        conn.execute(
            "UPDATE task_owner_instructions SET status='unroutable' WHERE id=?",
            (instruction.id,),
        )
        conn.commit()
    finally:
        conn.close()
    before = _statuses(db_path, ids)

    result = handle_reaction(_reaction(), config=_config())

    assert result.reason == "pending_owner_instructions"
    assert _statuses(db_path, ids) == before
    conn = kb.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='cancel_thread_work'"
        ).fetchone()[0] == 0
        unresolved = kb.get_owner_instruction(conn, instruction.id)
        assert unresolved is not None
        assert unresolved.status == "unroutable"
    finally:
        conn.close()


def test_trash_fails_closed_on_external_dependent(ghost_topology):
    db_path, ids = ghost_topology
    conn = kb.connect(db_path)
    try:
        outside = kb.create_task(
            conn,
            title="External dependent",
            body="body",
            assignee="ops",
            created_by="test",
            triage=True,
        )
        conn.execute(
            "INSERT INTO task_links(parent_id,child_id) VALUES (?,?)",
            (ids["generated_a"], outside),
        )
        conn.commit()
    finally:
        conn.close()
    before = _statuses(db_path, ids)
    assert handle_reaction(_reaction(), config=_config()).reason == "external_dependents"
    assert _statuses(db_path, ids) == before


def test_member_dependency_edge_is_not_ownership_without_transition_provenance(
    ghost_topology,
):
    db_path, ids = ghost_topology
    conn = kb.connect(db_path)
    try:
        unrelated = kb.create_task(
            conn,
            title="Unrelated dependent of represented card",
            body="body",
            assignee="ops",
            created_by="test",
            triage=True,
        )
        conn.execute(
            "INSERT INTO task_links(parent_id,child_id) VALUES (?,?)",
            (ids["bound"], unrelated),
        )
        conn.commit()
    finally:
        conn.close()
    before = _statuses(db_path, ids)

    result = handle_reaction(_reaction(), config=_config())

    assert result.reason == "external_dependents"
    assert _statuses(db_path, ids) == before
    conn = kb.connect(db_path)
    try:
        task = kb.get_task(conn, unrelated)
        assert task is not None
        assert task.status == "triage"
    finally:
        conn.close()


def test_cross_board_transition_same_task_id_fails_without_mutation(ghost_topology):
    db_path, ids = ghost_topology
    mirror_conn = connect_mirror(mirror_db_path("default"))
    try:
        mirror_conn.execute(
            "UPDATE mirror_binding_transitions SET new_card_metadata=? "
            "WHERE transition_key='test:bound-to-root'",
            (json.dumps({"board_slug": "other", "task_id": ids["shared"]}),),
        )
        mirror_conn.commit()
    finally:
        mirror_conn.close()
    before = _statuses(db_path, ids)

    result = handle_reaction(_reaction(), config=_config())

    assert result.reason == "missing_provenance"
    assert _statuses(db_path, ids) == before
    conn = kb.connect(db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='cancel_thread_work'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_snapshot_retains_decomposition_provenance_beyond_recent_window(ghost_topology):
    db_path, ids = ghost_topology
    conn = kb.connect(db_path)
    try:
        for index in range(11):
            conn.execute(
                "INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                (ids["root"], f"later_{index}", None, 100 + index),
            )
        conn.commit()
    finally:
        conn.close()
    snapshot = load_board_snapshot("default")
    assert any(
        event["kind"] == "decomposed"
        for event in snapshot.recent_events[ids["root"]]
    )
