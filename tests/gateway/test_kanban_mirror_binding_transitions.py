from concurrent.futures import ThreadPoolExecutor

import pytest

from plugins.platforms.discord.kanban_mirror.state import (
    active_thread_binding, add_member, authorize_starter_update,
    backfill_legacy_bindings, confirm_binding_transition, connect_mirror,
    create_initiative, get_binding_transition, pending_binding_transition,
    prepare_binding_transition, set_thread, verify_starter_revision,
)


def seed(path):
    conn = connect_mirror(path)
    create_initiative(conn, "init", "Fixture")
    add_member(conn, "init", "old")
    set_thread(conn, "init", "thread", "starter")
    backfill_legacy_bindings(conn, "board")
    return conn


def args(key="move"):
    return dict(transition_key=key, thread_id="thread",
                old_card_metadata={"board_slug": "board", "task_id": "old", "title": "Old"},
                new_card_metadata={"board_slug": "board", "task_id": "new", "title": "New"},
                transition_payload={"content": "Old -> New", "actor": "Ops"},
                starter_payload={"title": "New", "body": "next", "tags": ["active"]})


def test_prepare_is_recoverable_frozen_and_keeps_old_authoritative(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    first = prepare_binding_transition(conn, **args())
    assert first.state == "prepared"
    assert pending_binding_transition(conn, "thread") == first
    assert get_binding_transition(conn, "move") == first
    assert active_thread_binding(conn, "thread").task_id == "old"
    assert prepare_binding_transition(conn, **args()) == first
    changed = args(); changed["transition_payload"] = {"content": "changed"}
    with pytest.raises(ValueError, match="frozen"):
        prepare_binding_transition(conn, **changed)
    with pytest.raises(ValueError, match="not authorized"):
        authorize_starter_update(conn, "move")


def test_confirmation_atomically_switches_and_is_idempotent(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    prepare_binding_transition(conn, **args())
    confirmed = confirm_binding_transition(conn, "move", "discord-1")
    assert confirmed.state == "message_confirmed"
    assert active_thread_binding(conn, "thread").task_id == "new"
    epochs = conn.execute("SELECT task_id,state,transition_message_id FROM mirror_binding_epochs ORDER BY sequence").fetchall()
    assert [tuple(r) for r in epochs] == [("old", "closed", "discord-1"), ("new", "open", None)]
    assert confirm_binding_transition(conn, "move", "discord-1") == confirmed
    assert conn.execute("SELECT count(*) FROM mirror_binding_epochs").fetchone()[0] == 2
    with pytest.raises(ValueError, match="different message"):
        confirm_binding_transition(conn, "move", "discord-2")


def test_partial_failure_and_starter_revision_verification(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    prepare_binding_transition(conn, **args())
    with pytest.raises(ValueError):
        confirm_binding_transition(conn, "move", "")
    assert active_thread_binding(conn, "thread").task_id == "old"
    confirm_binding_transition(conn, "move", "discord-1")
    payload, expected = authorize_starter_update(conn, "move")
    assert payload["title"] == "New"
    with pytest.raises(ValueError, match="does not match"):
        verify_starter_revision(conn, "move", "wrong")
    done = verify_starter_revision(conn, "move", expected)
    assert done.state == "starter_verified"
    assert verify_starter_revision(conn, "move", expected).starter_verified_at == done.starter_verified_at
    assert active_thread_binding(conn, "thread").starter_revision_hash == expected


def test_mismatch_fails_closed_and_concurrent_prepares_serialize(tmp_path):
    path = tmp_path / "mirror.db"; conn = seed(path); conn.close()
    bad = args(); bad["old_card_metadata"]["task_id"] = "other"
    c = connect_mirror(path)
    with pytest.raises(ValueError, match="does not match"):
        prepare_binding_transition(c, **bad)
    assert active_thread_binding(c, "thread").task_id == "old"; c.close()

    def run(key):
        worker = connect_mirror(path)
        try:
            return prepare_binding_transition(worker, **args(key)).transition_key
        except Exception as exc:
            return type(exc).__name__
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("one", "two")))
    assert sum(r in {"one", "two"} for r in results) == 1
    assert len(results) == 2
    check = connect_mirror(path)
    assert active_thread_binding(check, "thread").task_id == "old"
    assert pending_binding_transition(check, "thread") is not None
