from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from plugins.platforms.discord.kanban_mirror import repair as repair_module
from plugins.platforms.discord.kanban_mirror.conversation_log import record_conversation_event
from plugins.platforms.discord.kanban_mirror.outbox import OutboundEnvelope, enqueue
from plugins.platforms.discord.kanban_mirror.reconciliation import (
    ExpectedThread, ObservedDigest, ObservedThread, list_reconciliation_findings,
    reconcile_mirror_state, reconciliation_report, resolve_thread_quarantine,
)
from plugins.platforms.discord.kanban_mirror.repair import (
    resolve_recoverable_quarantines,
    resolve_verified_historical_quarantines,
)
from plugins.platforms.discord.kanban_mirror.state import (
    BoardSnapshot, Card, active_thread_binding, add_member, backfill_legacy_bindings, connect_mirror,
    create_initiative, is_thread_quarantined, prepare_binding_transition,
    resolve_thread_task, set_archived, set_thread,
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


def migration_snapshot(status="done"):
    card = Card(
        id="task", title="Card", body="", status=status, priority=0,
        assignee="", branch_name="", workspace_kind="", created_by="",
        created_at=1, completed_at=2 if status in {"done", "archived"} else None,
        last_failure_error="", result="",
    )
    return BoardSnapshot(
        cards={"task": card}, children={}, parents={}, recent_comments={}, recent_events={},
    )


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


def test_complete_clean_observation_resolves_only_deterministic_stale_quarantine(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    reconcile_mirror_state(conn, observed_threads=observed(), cards=[], now=10)
    assert is_thread_quarantined(conn, "thread")
    assert reconcile_mirror_state(conn, observed_threads=observed(), cards=[("board", "task")], now=20) == []

    resolved = resolve_recoverable_quarantines(
        conn, observed_thread_ids={"thread"}, cards={"task"}, now=21,
    )

    assert resolved == ["thread"]
    assert not is_thread_quarantined(conn, "thread")


def test_resolved_nonrecoverable_revision_mismatch_does_not_hold_clean_quarantine(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,binding_key,task_id,evidence,evidence_hash,
         first_seen_at,last_seen_at,resolved_at)
        VALUES ('revision','error','starter.revision_mismatch','thread','binding:thread:1',
                'task','{}','h',10,20,20)""")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at)
        VALUES ('thread',1,10,10)""")
    conn.commit()

    assert resolve_recoverable_quarantines(
        conn, observed_thread_ids={'thread'}, cards={'task'}, now=21,
    ) == ['thread']
    assert not is_thread_quarantined(conn, 'thread')


def test_verified_historical_resolver_keeps_live_or_ambiguous_records_quarantined(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    set_archived(conn, "init", 10)
    conn.execute("UPDATE mirror_members SET last_status='done' WHERE initiative_id='init'")
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,evidence,evidence_hash,first_seen_at,last_seen_at)
        VALUES ('old','warning','thread.premature_archive','thread','{}','h',10,10)""")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at) VALUES ('thread',1,10,10)""")
    create_initiative(conn, "live", "Live")
    set_thread(conn, "live", "live-thread", "starter")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at) VALUES ('live-thread',1,10,10)""")
    conn.commit()

    assert resolve_verified_historical_quarantines(
        conn, migration_snapshot(), fresh_archived_thread_ids={"thread"}, now=20,
    ) == ["thread"]
    assert not is_thread_quarantined(conn, "thread")
    assert is_thread_quarantined(conn, "live-thread")
    assert conn.execute("SELECT resolved_at FROM mirror_reconciliation_findings WHERE finding_key='old'").fetchone()[0] == 20


def test_verified_historical_migration_rejects_archived_revision_history(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    set_archived(conn, "init", 10)
    conn.execute("UPDATE mirror_members SET last_status='archived' WHERE initiative_id='init'")
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,evidence,evidence_hash,first_seen_at,last_seen_at)
        VALUES ('old-revision','error','starter.revision_mismatch','thread','{}','h',10,10)""")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at) VALUES ('thread',1,10,10)""")
    conn.commit()

    assert resolve_verified_historical_quarantines(
        conn, migration_snapshot(), fresh_archived_thread_ids={"thread"}, now=20,
    ) == []
    assert is_thread_quarantined(conn, "thread")


def test_verified_historical_migration_rejects_without_fresh_archived_proof(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    set_archived(conn, "init", 10)
    conn.execute("UPDATE mirror_members SET last_status='done' WHERE initiative_id='init'")
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,evidence,evidence_hash,first_seen_at,last_seen_at)
        VALUES ('old','critical','binding.open_count','thread','{}','h',10,10)""")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at) VALUES ('thread',1,10,10)""")
    conn.commit()

    assert resolve_verified_historical_quarantines(
        conn, migration_snapshot(), fresh_archived_thread_ids=set(), now=20,
    ) == []
    assert is_thread_quarantined(conn, "thread")
    assert tuple(conn.execute(
        "SELECT state,ended_at FROM mirror_binding_epochs WHERE thread_id='thread'"
    ).fetchone()) == ("open", None)


def test_verified_historical_migration_backs_up_closes_epochs_and_is_idempotent(tmp_path):
    path = tmp_path / "mirror.db"
    conn = seed(path)
    set_archived(conn, "init", 10)
    conn.execute("UPDATE mirror_members SET last_status='done' WHERE initiative_id='init'")
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,binding_key,task_id,evidence,evidence_hash,
         first_seen_at,last_seen_at)
        VALUES ('old-open','critical','binding.open_count','thread',NULL,NULL,'{}','h',10,10),
               ('old-archive','critical','thread.premature_archive','thread','binding:thread:1','task','{}','h2',10,10)""")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at) VALUES ('thread',1,10,10)""")
    conn.commit()

    assert resolve_verified_historical_quarantines(
        conn, migration_snapshot(), fresh_archived_thread_ids={"thread"}, now=20,
    ) == ["thread"]

    epoch = conn.execute(
        "SELECT state,ended_at FROM mirror_binding_epochs WHERE thread_id='thread'"
    ).fetchone()
    assert tuple(epoch) == ("historical_closed", 10)
    backup = conn.execute(
        "SELECT backup_path FROM mirror_historical_quarantine_migrations WHERE thread_id='thread'"
    ).fetchone()[0]
    assert backup and Path(backup).is_file()
    restored = sqlite3.connect(backup)
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute(
            "SELECT state,ended_at FROM mirror_binding_epochs WHERE thread_id='thread'"
        ).fetchone() == ("open", None)
        assert restored.execute(
            "SELECT resolved_at FROM mirror_reconciliation_findings WHERE finding_key='old-archive'"
        ).fetchone()[0] is None
        assert restored.execute(
            "SELECT resolved_at FROM mirror_thread_quarantine WHERE thread_id='thread'"
        ).fetchone()[0] is None
        assert restored.execute(
            "SELECT COUNT(*) FROM mirror_historical_quarantine_migration_runs"
        ).fetchone()[0] == 0
    finally:
        restored.close()
    run = conn.execute(
        "SELECT migrated_count,backup_path FROM mirror_historical_quarantine_migration_runs"
    ).fetchone()
    assert tuple(run) == (1, backup)
    assert not is_thread_quarantined(conn, "thread")
    assert resolve_verified_historical_quarantines(
        conn, migration_snapshot(), fresh_archived_thread_ids={"thread"}, now=30,
    ) == []


def test_verified_historical_migration_holds_write_lock_before_backup(tmp_path, monkeypatch):
    path = tmp_path / "mirror.db"
    conn = seed(path)
    set_archived(conn, "init", 10)
    conn.execute("UPDATE mirror_members SET last_status='done' WHERE initiative_id='init'")
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,evidence,evidence_hash,first_seen_at,last_seen_at)
        VALUES ('old','critical','binding.open_count','thread','{}','h',10,10)""")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at) VALUES ('thread',1,10,10)""")
    conn.commit()
    original_backup = repair_module._create_verified_backup
    locked = []

    def backup_probe(locked_conn, db_path, stamp):
        writer = sqlite3.connect(str(path), timeout=0.0)
        try:
            try:
                writer.execute(
                    "INSERT INTO mirror_thread_quarantine(thread_id,needs_repair,quarantined_at,updated_at) "
                    "VALUES ('concurrent',1,1,1)"
                )
            except sqlite3.OperationalError as exc:
                locked.append("locked" in str(exc).lower())
            else:
                writer.commit()
                locked.append(False)
        finally:
            writer.close()
        return original_backup(locked_conn, db_path, stamp)

    monkeypatch.setattr(repair_module, "_create_verified_backup", backup_probe)

    assert resolve_verified_historical_quarantines(
        conn, migration_snapshot(), fresh_archived_thread_ids={"thread"}, now=20,
    ) == ["thread"]
    assert locked == [True]
    assert conn.execute(
        "SELECT 1 FROM mirror_thread_quarantine WHERE thread_id='concurrent'"
    ).fetchone() is None


def test_verified_historical_migration_rolls_back_when_backup_fails(tmp_path, monkeypatch):
    conn = seed(tmp_path / "mirror.db")
    set_archived(conn, "init", 10)
    conn.execute("UPDATE mirror_members SET last_status='done' WHERE initiative_id='init'")
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,evidence,evidence_hash,first_seen_at,last_seen_at)
        VALUES ('old','critical','binding.open_count','thread','{}','h',10,10)""")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at) VALUES ('thread',1,10,10)""")
    conn.commit()

    def fail_backup(*args, **kwargs):
        raise RuntimeError("backup failed")

    monkeypatch.setattr(repair_module, "_create_verified_backup", fail_backup)

    try:
        resolve_verified_historical_quarantines(
            conn, migration_snapshot(), fresh_archived_thread_ids={"thread"}, now=20,
        )
    except RuntimeError as exc:
        assert "backup failed" in str(exc)
    else:
        raise AssertionError("migration should fail closed when backup fails")

    assert tuple(conn.execute(
        "SELECT state,ended_at FROM mirror_binding_epochs WHERE thread_id='thread'"
    ).fetchone()) == ("open", None)
    assert conn.execute(
        "SELECT resolved_at FROM mirror_reconciliation_findings WHERE finding_key='old'"
    ).fetchone()[0] is None
    assert is_thread_quarantined(conn, "thread")
    assert conn.execute("SELECT COUNT(*) FROM mirror_historical_quarantine_migration_runs").fetchone()[0] == 0


def test_verified_historical_migration_revalidation_failure_rolls_back_batch(tmp_path, monkeypatch):
    path = tmp_path / "mirror.db"
    conn = seed(path)
    set_archived(conn, "init", 10)
    conn.execute("UPDATE mirror_members SET last_status='done' WHERE initiative_id='init'")
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,evidence,evidence_hash,first_seen_at,last_seen_at)
        VALUES ('old-one','critical','binding.open_count','thread','{}','h',10,10)""")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at) VALUES ('thread',1,10,10)""")
    create_initiative(conn, "init2", "Card 2")
    add_member(conn, "init2", "task2")
    set_thread(conn, "init2", "thread2", "starter2")
    backfill_legacy_bindings(conn, "board")
    set_archived(conn, "init2", 11)
    conn.execute("UPDATE mirror_members SET last_status='done' WHERE initiative_id='init2'")
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,evidence,evidence_hash,first_seen_at,last_seen_at)
        VALUES ('old-two','critical','binding.open_count','thread2','{}','h2',10,10)""")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at) VALUES ('thread2',1,10,10)""")
    conn.commit()
    snapshot = BoardSnapshot(
        cards={
            "task": migration_snapshot().cards["task"],
            "task2": Card(
                id="task2", title="Card 2", body="", status="done", priority=0,
                assignee="", branch_name="", workspace_kind="", created_by="",
                created_at=1, completed_at=2, last_failure_error="", result="",
            ),
        },
        children={}, parents={}, recent_comments={}, recent_events={},
    )
    calls = []

    def terminal_until_second_revalidation(initiative, board_snapshot):
        calls.append(initiative.id)
        return calls != ["init", "init2", "init", "init2"]

    monkeypatch.setattr(
        "plugins.platforms.discord.kanban_mirror.repair.all_work_items_terminal",
        terminal_until_second_revalidation,
    )

    try:
        resolve_verified_historical_quarantines(
            conn, snapshot, fresh_archived_thread_ids={"thread", "thread2"}, now=20,
        )
    except RuntimeError as exc:
        assert "eligibility changed" in str(exc)
    else:
        raise AssertionError("expected historical migration to fail closed")

    assert conn.execute("SELECT count(*) FROM mirror_historical_quarantine_migration_runs").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM mirror_historical_quarantine_migrations").fetchone()[0] == 0
    assert {
        tuple(row) for row in conn.execute(
            "SELECT thread_id,state FROM mirror_binding_epochs ORDER BY thread_id"
        )
    } == {("thread", "open"), ("thread2", "open")}
    assert is_thread_quarantined(conn, "thread")
    assert is_thread_quarantined(conn, "thread2")


def test_verified_historical_migration_rejects_member_without_terminal_status(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    set_archived(conn, "init", 10)
    conn.execute("UPDATE mirror_members SET last_status='done' WHERE initiative_id='init'")
    add_member(conn, "init", "unknown-status")
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,evidence,evidence_hash,first_seen_at,last_seen_at)
        VALUES ('old','critical','binding.open_count','thread','{}','h',10,10)""")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at) VALUES ('thread',1,10,10)""")
    conn.commit()

    assert resolve_verified_historical_quarantines(
        conn, migration_snapshot(), fresh_archived_thread_ids={"thread"}, now=20,
    ) == []
    assert is_thread_quarantined(conn, "thread")


def test_verified_historical_migration_rejects_stale_terminal_member_state(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    set_archived(conn, "init", 10)
    conn.execute("UPDATE mirror_members SET last_status='done' WHERE initiative_id='init'")
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,evidence,evidence_hash,first_seen_at,last_seen_at)
        VALUES ('old','critical','binding.open_count','thread','{}','h',10,10)""")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at) VALUES ('thread',1,10,10)""")
    conn.commit()

    assert resolve_verified_historical_quarantines(
        conn, migration_snapshot("running"), fresh_archived_thread_ids={"thread"}, now=20,
    ) == []
    assert is_thread_quarantined(conn, "thread")


def test_migrated_historical_thread_does_not_reopen_binding_or_archive_warnings(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    set_archived(conn, "init", 10)
    conn.execute("UPDATE mirror_members SET last_status='done' WHERE initiative_id='init'")
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,evidence,evidence_hash,first_seen_at,last_seen_at)
        VALUES ('old','critical','binding.open_count','thread','{}','h',10,10)""")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at) VALUES ('thread',1,10,10)""")
    conn.commit()
    assert resolve_verified_historical_quarantines(
        conn, migration_snapshot(), fresh_archived_thread_ids={"thread"}, now=20,
    ) == ["thread"]

    findings = reconcile_mirror_state(
        conn,
        observed_threads={"thread": ObservedThread("thread", "starter", None, title="Card", tags=("done",), archived=True)},
        cards=[("board", "task")],
        expected_threads={"thread": ExpectedThread("Card", ("done",), True)},
        now=30,
    )

    assert {f.code for f in findings} == set()
    assert not is_thread_quarantined(conn, "thread")

    findings = reconcile_mirror_state(
        conn,
        observed_threads={"thread": ObservedThread("thread", "starter", None, title="Card", tags=("running",), archived=False)},
        cards=[("board", "task")],
        expected_threads={"thread": ExpectedThread("Card", ("running",), False)},
        now=40,
    )
    assert {f.code for f in findings} == {"binding.open_count"}
    assert is_thread_quarantined(conn, "thread")


def test_clean_scan_does_not_resolve_quarantine_without_a_valid_observed_binding(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    reconcile_mirror_state(conn, observed_threads=observed(), cards=[], now=10)
    reconcile_mirror_state(conn, observed_threads=observed(), cards=[("board", "task")], now=20)

    assert resolve_recoverable_quarantines(
        conn, observed_thread_ids=set(), cards={"task"}, now=21,
    ) == []
    assert is_thread_quarantined(conn, "thread")


def test_clean_scan_never_auto_resolves_successor_ambiguity_quarantine(tmp_path):
    conn = seed(tmp_path / "mirror.db")
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,evidence,evidence_hash,
         first_seen_at,last_seen_at,resolved_at)
        VALUES ('successor','error','successor.selection_ambiguous','thread','{}','h',10,10,20)""")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at)
        VALUES ('thread',1,10,10)""")
    conn.commit()

    assert resolve_recoverable_quarantines(
        conn,observed_thread_ids={'thread'},cards={'task'},now=21,
    )==[]
    assert is_thread_quarantined(conn,'thread')


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


def test_legacy_archived_orphan_is_not_quarantined_as_premature(tmp_path):
    conn = connect_mirror(tmp_path / "mirror.db")
    create_initiative(conn, "legacy", "Retired test post")
    set_thread(conn, "legacy", "thread", "starter")
    set_archived(conn, "legacy", 5)

    findings = reconcile_mirror_state(
        conn,
        observed_threads={
            "thread": ObservedThread(
                "thread", "starter", None, title="Retired test post", tags=("done",),
                archived=True,
            )
        },
        cards=[],
        expected_threads={
            "thread": ExpectedThread("Retired test post", ("done",), True)
        },
        now=10,
    )

    assert findings == []
    assert not is_thread_quarantined(conn, "thread")


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
    clean_live = {"thread": ObservedThread("thread", "starter", None, title="Card",
        tags=("active", "done"), archived=True)}
    conn.execute("UPDATE mirror_terminal_lifecycles SET state='archived'"); conn.commit()
    assert reconcile_mirror_state(conn, observed_threads=clean_live, cards=[("board", "task")],
        expected_threads=expected, observed_digest=ObservedDigest("digest", "Board", True), now=30) == []
    assert all(f.resolved_at == 30 for f in list_reconciliation_findings(conn))


def test_completed_lifecycle_reopen_is_report_only(tmp_path):
    conn = seed(tmp_path / "mirror.db"); add_lifecycle(conn, "archived")
    live = {"thread": ObservedThread("thread", "starter", None, title="Card", tags=("done",), archived=False)}
    findings = reconcile_mirror_state(conn, observed_threads=live, cards=[("board", "task")],
        expected_threads={"thread": ExpectedThread("Card", ("done",), True)},
        observed_digest=ObservedDigest("digest", "<!-- terminal:thread -->\n- [2026-07-12](https://discord/thread) — shipped", True))
    assert {f.code for f in findings} == {"thread.unexpected_reopen"}
    assert not is_thread_quarantined(conn, "thread")
