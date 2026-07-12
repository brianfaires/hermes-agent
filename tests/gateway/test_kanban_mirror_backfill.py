from __future__ import annotations

import asyncio
import sqlite3

import pytest

from gateway.kanban_mirror.backfill import DiscordBackfillIngestor, DiscordInbound, HistoryPage
from gateway.kanban_mirror.state import connect_mirror


class FakeHistory:
    _NEVER = object()

    def __init__(self, messages, fail_after=_NEVER, gate=None):
        self.messages, self.fail_after, self.gate = messages, fail_after, gate
        self.calls = []

    async def fetch_after(self, thread_id, after, limit):
        self.calls.append((thread_id, after, limit))
        if self.gate:
            await self.gate(thread_id)
        if after == self.fail_after:
            raise OSError("history unavailable")
        left = [m for m in self.messages if m.thread_id == thread_id and int(m.message_id) > int(after or 0)]
        return HistoryPage(list(reversed(left[:limit])), len(left) > limit)


@pytest.fixture
def conn(tmp_path):
    value = connect_mirror(tmp_path / "mirror.db")
    yield value
    value.close()


def message(mid, thread="10", content=None, **kw):
    return DiscordInbound(str(mid), thread, content or f"message {mid}", created_at=900, **kw)


def bind(conn, thread="10"):
    conn.execute("""INSERT INTO mirror_binding_epochs
        (binding_key,thread_id,board_slug,task_id,sequence,started_at,state)
        VALUES (?,?,?,?,1,1,'open')""", (f"binding-{thread}", thread, "fixture", f"card-{thread}"))
    conn.commit()


@pytest.mark.asyncio
async def test_ordered_pages_durable_cursor_and_live_overlap(conn):
    bind(conn)
    seen = []
    ingestor = DiscordBackfillIngestor(conn, page_size=2, processor=lambda m, _: seen.append(m.message_id), clock=lambda: 1000)
    await ingestor.ingest_live(message(101))
    result = await ingestor.backfill("10", FakeHistory([message(101), message(102), message(103)]))
    assert seen == ["101", "102", "103"]
    assert (result.ingested, result.pages, result.cursor) == (2, 1, "103")
    assert DiscordBackfillIngestor(conn).cursor("10") == "103"
    assert conn.execute("SELECT COUNT(*) FROM mirror_conversation_events").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_partial_page_failure_retries_without_skipping(conn):
    bind(conn)
    ingestor = DiscordBackfillIngestor(conn, page_size=3, clock=lambda: 1000)
    original, failed = ingestor._persist, False

    async def crash_once(item, via):
        nonlocal failed
        if item.message_id == "102" and not failed:
            failed = True
            raise sqlite3.OperationalError("fixture write failure")
        return await original(item, via)

    ingestor._persist = crash_once
    history = FakeHistory([message(101), message(102), message(103)])
    with pytest.raises(sqlite3.OperationalError):
        await ingestor.backfill("10", history)
    assert ingestor.cursor("10") == "101"
    await ingestor.backfill("10", history)
    assert ingestor.cursor("10") == "103"
    assert [r[0] for r in conn.execute("SELECT discord_message_id FROM mirror_conversation_events ORDER BY id")] == ["101", "102", "103"]


@pytest.mark.asyncio
async def test_noise_malformed_unmapped_quarantine_preserved_and_advanced(conn):
    bind(conn, "q")
    conn.execute("INSERT INTO mirror_thread_quarantine(thread_id,quarantined_at,updated_at) VALUES ('q',1,1)")
    conn.commit()
    ingestor = DiscordBackfillIngestor(conn)
    await ingestor.ingest_live(message(11, relevant=False))
    await ingestor.ingest_live(DiscordInbound("12", "10", None, created_at=900))
    await ingestor.ingest_live(message(13, "unmapped"))
    await ingestor.ingest_live(message(14, "q"))
    rows = conn.execute("SELECT classification,processing_status FROM mirror_discord_inbound_state ORDER BY CAST(discord_message_id AS INT)").fetchall()
    assert [tuple(r) for r in rows] == [("noise", "processed"), ("malformed", "processed"), ("unmapped", "processed"), ("quarantined", "processed")]
    assert conn.execute("SELECT COUNT(*) FROM mirror_conversation_events").fetchone()[0] == 4


@pytest.mark.asyncio
async def test_fetch_failure_cursor_metrics_and_bounds(conn):
    bind(conn)
    ingestor = DiscordBackfillIngestor(conn, page_size=1, max_pages=3, clock=lambda: 1000)
    with pytest.raises(OSError):
        await ingestor.backfill("10", FakeHistory([message(101), message(102)], fail_after="101"))
    assert ingestor.metrics("10", latest_message_id="105") == {"cursor": "101", "message_id_lag": 4, "oldest_unprocessed_at": 1000, "backlog_limited": 0}
    bounded = DiscordBackfillIngestor(conn, page_size=1, max_pages=1, max_age_seconds=10, clock=lambda: 1000)
    result = await bounded.backfill("10", FakeHistory([message(101), message(102)]))
    assert result.limited and result.cursor == "101"
    assert bounded.metrics("10")["backlog_limited"] == 1


@pytest.mark.asyncio
async def test_per_thread_serialization_and_cross_thread_concurrency(conn):
    bind(conn, "10"); bind(conn, "20")
    entered, release, active, overlap = asyncio.Event(), asyncio.Event(), set(), []

    async def gate(thread):
        active.add(thread); overlap.append(set(active)); entered.set()
        await release.wait(); active.remove(thread)

    ingestor = DiscordBackfillIngestor(conn)
    first = asyncio.create_task(ingestor.backfill("10", FakeHistory([message(101)], gate=gate)))
    await entered.wait()
    same = asyncio.create_task(ingestor.backfill("10", FakeHistory([], gate=gate)))
    other = asyncio.create_task(ingestor.backfill("20", FakeHistory([message(201, "20")], gate=gate)))
    await asyncio.sleep(0)
    assert {"10", "20"} in overlap
    release.set()
    await asyncio.gather(first, same, other)


@pytest.mark.asyncio
async def test_existing_ledger_event_and_processor_failure_remain_safe(conn):
    bind(conn)
    conn.execute("""INSERT INTO mirror_conversation_events
        (discord_message_id,thread_id,binding_key,event_class,author_label,content,recorded_at)
        VALUES ('101','10','binding-10','conversation.human','human','already handled',1)""")
    conn.commit()
    calls = []

    def failing_processor(item, classification):
        calls.append((item.message_id, classification))
        raise RuntimeError("handler down")

    ingestor = DiscordBackfillIngestor(conn, processor=failing_processor)
    await ingestor.ingest_live(message(101))
    await ingestor.ingest_live(message(102))
    assert calls == [("102", "pending")]
    rows = conn.execute("""SELECT discord_message_id,classification,processing_status
        FROM mirror_discord_inbound_state ORDER BY discord_message_id""").fetchall()
    assert [tuple(row) for row in rows] == [
        ("101", "already_recorded", "processed"), ("102", "pending", "pending")
    ]
