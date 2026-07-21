from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from plugins.platforms.discord.kanban_mirror.conversation_log import record_conversation_event
from plugins.platforms.discord.kanban_mirror.outbox import OutboundEnvelope, enqueue
from plugins.platforms.discord.kanban_mirror.reconciliation import (
    ExpectedThread, ObservedDigest, ObservedThread, list_reconciliation_findings,
    reconcile_mirror_state, reconciliation_report, resolve_thread_quarantine,
)
from plugins.platforms.discord.kanban_mirror.state import (
    active_thread_binding, add_member, backfill_legacy_bindings, connect_mirror,
    create_initiative, is_thread_quarantined, prepare_binding_transition,
    resolve_thread_task, set_thread,
)


def seed(path):
    conn = connect_mirror(path)
    create_initiative(conn, "init", "Card")
    add_member(conn, "init", "task")
    set_thread(conn, "init", "thread", "starter")
    backfill_legacy_bindings(conn, "board")
    return conn


def observed(revision=None, messages=frozenset()):
    return {"thread": ObservedThread("thread", "starter", revision, messages)}


def test_findings_are_idempotent_update_evidence_and_preserve_resolved_history(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    first = reconcile_mirror_state(conn, observed_threads=observed("wrong"), cards=[], now=10)
    first_keys = {f.finding_key for f in first}
    assert {f.code for f in first} == {"binding.card_missing"}
    again = reconcile_mirror_state(conn, observed_threads=observed("different"), cards=[], now=20)
    assert {f.finding_key for f in again} == first_keys
    assert again[0].first_seen_at == 10 and again[0].last_seen_at == 20
    assert again[0].evidence_hash == first[0].evidence_hash  # card evidence is stable

    assert reconcile_mirror_state(conn, observed_threads=observed(), cards=[("board", "task")], now=30) == []
    history = list_reconciliation_findings(conn)
    assert len(history) == 1 and history[0].resolved_at == 30
    assert is_thread_quarantined(conn, "thread")  # clean scan alone is not acknowledgement
    assert resolve_thread_quarantine(conn, "thread", now=31)
    assert not is_thread_quarantined(conn, "thread")


def test_quarantine_fails_closed_without_destroying_discussion_or_state(tmp_path):
    path = tmp_path / "board" / "mirror.db"
    conn = seed(path)
    before = [tuple(r) for r in conn.execute("SELECT * FROM mirror_binding_epochs")]
    reconcile_mirror_state(conn, observed_threads=observed(), cards=[], now=10)
    assert is_thread_quarantined(conn, "thread")
    assert active_thread_binding(conn, "thread") is None
    assert resolve_thread_task(path, "forum", "thread") is None
    event = record_conversation_event(conn, discord_message_id="m1", thread_id="thread", binding_key=None,
                                      event_class="conversation.human", author_label="User", content="preserved")
    assert event.content == "preserved"
    assert [tuple(r) for r in conn.execute("SELECT * FROM mirror_binding_epochs")] == before
    assert conn.execute("SELECT count(*) FROM mirror_members").fetchone()[0] == 1


def test_pending_transition_and_changed_starter_are_visible_without_repair(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    prepare_binding_transition(
        conn, transition_key="move", thread_id="thread",
        old_card_metadata={"board_slug": "board", "task_id": "task"},
        new_card_metadata={"board_slug": "board", "task_id": "next"},
        transition_payload={"content": "moving"}, starter_payload={"title": "Next"},
    )
    findings = reconcile_mirror_state(
        conn, observed_threads=observed("changed"),
        cards=[("board", "task"), ("board", "next")], now=10,
    )
    assert {f.code for f in findings} == {
        "transition.pending", "starter.changed_without_transition_confirmation",
    }
    assert is_thread_quarantined(conn, "thread")
    assert conn.execute("SELECT state FROM mirror_binding_transitions").fetchone()[0] == "prepared"
    assert conn.execute("SELECT task_id FROM mirror_binding_epochs WHERE state='open'").fetchone()[0] == "task"


def test_pending_and_failed_deliveries_are_reported_but_do_not_quarantine(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    enqueue(conn, OutboundEnvelope(profile="ops", thread_id="thread", reply_to_message_id=None,
                                    content="reply", attachments=(), correlation_id="corr"))
    conn.execute("""INSERT INTO mirror_conversation_deliveries
        (operation_id,trigger_discord_message_id,thread_id,task_id,mode,payload,payload_hash,status,attempt_count,last_error,created_at,updated_at)
        VALUES ('log','cmd','thread','task','current','body','hash','failed',2,'offline',1,1)""")
    conn.commit()
    findings = reconcile_mirror_state(conn, observed_threads=observed(), cards=[("board", "task")], now=10)
    assert {f.code for f in findings} == {"delivery.outbound_pending", "delivery.log_failed"}
    assert not is_thread_quarantined(conn, "thread")
    report = reconciliation_report(conn)
    assert report["open_count"] == 2 and report["quarantined_threads"] == []


def test_concurrent_scans_have_one_durable_finding(tmp_path):
    path = tmp_path / "mirror.db"
    seed(path).close()

    def scan(now):
        conn = connect_mirror(path)
        try:
            return len(reconcile_mirror_state(conn, observed_threads=observed(), cards=[], now=now))
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(scan, (10, 20))) == [1, 1]
    conn = connect_mirror(path)
    assert conn.execute("SELECT count(*) FROM mirror_reconciliation_findings").fetchone()[0] == 1


def add_lifecycle(conn, state="tag_confirmed", due=5):
    binding = active_thread_binding(conn, "thread")
    payload = {"summary": {}, "digest": {"thread_id": "thread", "outcome": "shipped",
        "date_range": {"end": "2026-07-12"}, "thread_link": "https://discord/thread"}}
    import json
    conn.execute("""INSERT INTO mirror_terminal_lifecycles
        (lifecycle_key,thread_id,binding_key,frozen_payload,frozen_hash,state,latest_activity_at,
         archive_due_at,prepared_at,updated_at) VALUES ('life','thread',?,?,?,?,0,?,1,1)""",
        (binding.binding_key, json.dumps(payload), "hash", state, due))
    conn.commit()


def test_live_metadata_drift_is_stable_and_only_premature_archive_quarantines(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    expected = {"thread": ExpectedThread("Expected", ("active",), False)}
    live = {"thread": ObservedThread("thread", "starter", None, title="Wrong", tags=("done",), archived=True)}
    first = reconcile_mirror_state(conn, observed_threads=live, cards=[("board", "task")],
                                   expected_threads=expected, now=10)
    assert {f.code for f in first} >= {"thread.title_mismatch", "thread.tags_mismatch",
        "thread.done_tag_unexpected", "thread.premature_archive"}
    assert is_thread_quarantined(conn, "thread")
    keys = {f.code: f.finding_key for f in first}
    second = reconcile_mirror_state(conn, observed_threads=live, cards=[("board", "task")],
                                    expected_threads=expected, now=20)
    assert {f.code: f.finding_key for f in second} == keys


def test_terminal_stages_digest_drift_partial_retention_and_resolution(tmp_path):
    conn = seed(tmp_path / "mirror.db"); add_lifecycle(conn)
    expected = {"thread": ExpectedThread("Card", ("active", "done"), True)}
    live = {"thread": ObservedThread("thread", "starter", None, title="Card", tags=("active",), archived=False)}
    digest = ObservedDigest("digest", "<!-- terminal:thread -->\n- [wrong](https://wrong) — wrong", False)
    findings = reconcile_mirror_state(conn, observed_threads=live, cards=[("board", "task")],
        expected_threads=expected, observed_digest=digest, now=10)
    codes = {f.code for f in findings}
    assert {"thread.done_tag_missing", "thread.terminal_unarchived", "digest.entry_stale",
        "digest.thread_link_mismatch", "digest.outcome_mismatch", "digest.date_hash_mismatch",
        "digest.unpinned"} <= codes
    assert not is_thread_quarantined(conn, "thread")
    partial = reconcile_mirror_state(conn, observed_threads={}, cards=[("board", "task")],
        expected_threads=expected, observed_digest=None, digest_observation_complete=False, now=20)
    assert codes <= {f.code for f in partial}
    good_line = "<!-- terminal:thread -->\n- [2026-07-12](https://discord/thread) — shipped"
    clean_live = {"thread": ObservedThread("thread", "starter", None, title="Card",
        tags=("active", "done"), archived=True)}
    conn.execute("UPDATE mirror_terminal_lifecycles SET state='archived'"); conn.commit()
    assert reconcile_mirror_state(conn, observed_threads=clean_live, cards=[("board", "task")],
        expected_threads=expected, observed_digest=ObservedDigest("digest", good_line, True), now=30) == []
    assert all(f.resolved_at == 30 for f in list_reconciliation_findings(conn))


def test_completed_lifecycle_reopen_is_report_only(tmp_path):
    conn = seed(tmp_path / "mirror.db"); add_lifecycle(conn, "archived")
    live = {"thread": ObservedThread("thread", "starter", None, title="Card", tags=("done",), archived=False)}
    findings = reconcile_mirror_state(conn, observed_threads=live, cards=[("board", "task")],
        expected_threads={"thread": ExpectedThread("Card", ("done",), True)},
        observed_digest=ObservedDigest("digest", "<!-- terminal:thread -->\n- [2026-07-12](https://discord/thread) — shipped", True))
    assert {f.code for f in findings} == {"thread.unexpected_reopen"}
    assert not is_thread_quarantined(conn, "thread")
