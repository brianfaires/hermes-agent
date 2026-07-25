from __future__ import annotations
import pytest
from plugins.platforms.discord.kanban_mirror.backfill import DiscordBackfillIngestor, DiscordInbound
from plugins.platforms.discord.kanban_mirror.inbound import PendingInboundRunner, ProcessResult
from plugins.platforms.discord.kanban_mirror.repair import recover_pending_inbound_bindings
from plugins.platforms.discord.kanban_mirror.outbox import OutboundEnvelope, enqueue
from plugins.platforms.discord.kanban_mirror.state import connect_mirror


def bind(conn, thread):
    conn.execute("INSERT INTO mirror_binding_epochs(binding_key,thread_id,board_slug,task_id,sequence,started_at,state) VALUES(?,?,?,?,1,1,'open')", (f"b-{thread}", thread, "x", f"c-{thread}")); conn.commit()

@pytest.mark.asyncio
async def test_crash_recovery_dispatch_once_then_outbox_ack(tmp_path):
    conn=connect_mirror(tmp_path/'m.db'); bind(conn,'1')
    await DiscordBackfillIngestor(conn, clock=lambda:100).ingest_live(DiscordInbound('10','1','frozen',forum_channel_id='9'))
    calls=[]
    async def handler(item): calls.append(item.payload['content']); return ProcessResult('routed', correlation_id='corr')
    clock=[100]
    runner=PendingInboundRunner(conn,handler,clock=lambda:clock[0])
    assert await runner.run_once()==1
    clock[0]=102; await runner.run_once()
    assert calls==['frozen']
    enqueue(conn,OutboundEnvelope('p','1','10','answer',(),'corr'))
    clock[0]=105; await runner.run_once()
    assert conn.execute("SELECT processing_status FROM mirror_discord_inbound_state").fetchone()[0]=='processed'

@pytest.mark.asyncio
async def test_per_thread_order_and_independence(tmp_path):
    conn=connect_mirror(tmp_path/'m.db'); bind(conn,'1'); bind(conn,'2')
    ing=DiscordBackfillIngestor(conn,clock=lambda:100)
    for mid,thread in [('10','1'),('11','1'),('20','2')]: await ing.ingest_live(DiscordInbound(mid,thread,mid))
    seen=[]
    async def handler(item): seen.append(item.message_id); return ProcessResult('disposition',disposition='filtered')
    runner=PendingInboundRunner(conn,handler,clock=lambda:100)
    assert await runner.run_once()==2 and seen==['10','20']
    assert await runner.run_once()==1 and seen[-1]=='11'

@pytest.mark.asyncio
async def test_outage_backoff_then_recovery(tmp_path):
    conn=connect_mirror(tmp_path/'m.db'); bind(conn,'1'); await DiscordBackfillIngestor(conn,clock=lambda:10).ingest_live(DiscordInbound('10','1','x'))
    now=[10]; up=[False]
    async def handler(_): return ProcessResult('disposition',disposition='accepted') if up[0] else ProcessResult('retry',detail='profile unavailable')
    runner=PendingInboundRunner(conn,handler,clock=lambda:now[0])
    await runner.run_once(); row=conn.execute("SELECT processing_status,last_error,next_attempt_at FROM mirror_discord_inbound_state").fetchone()
    assert tuple(row)==('pending','profile unavailable',12)
    assert await runner.run_once()==0
    up[0]=True; now[0]=12; await runner.run_once()
    assert conn.execute("SELECT processing_status FROM mirror_discord_inbound_state").fetchone()[0]=='processed'

@pytest.mark.asyncio
async def test_malformed_payload_is_quarantined(tmp_path):
    conn=connect_mirror(tmp_path/'m.db'); bind(conn,'1'); await DiscordBackfillIngestor(conn,clock=lambda:1).ingest_live(DiscordInbound('10','1','x'))
    conn.execute("UPDATE mirror_discord_inbound_state SET payload='{' "); conn.commit()
    runner=PendingInboundRunner(conn,lambda _: (_ for _ in ()).throw(AssertionError()),clock=lambda:2)
    await runner.run_once()
    assert conn.execute("SELECT disposition FROM mirror_discord_inbound_dispositions").fetchone()[0]=='quarantined_malformed'


def test_recovery_binds_uniquely_attributed_pending_event(tmp_path):
    conn=connect_mirror(tmp_path/'m.db')
    ing=DiscordBackfillIngestor(conn,clock=lambda:100)
    import asyncio
    asyncio.run(ing.ingest_live(DiscordInbound('10','1','later work',created_at=50)))
    bind(conn,'1')

    result=recover_pending_inbound_bindings(
        conn, cards={'c-1':('running',None)}, now=101,
    )

    assert result=={'rebound':1,'superseded':0,'deduplicated':0}
    event=conn.execute("SELECT binding_key,binding_task_id,binding_interval FROM mirror_conversation_events WHERE discord_message_id='10'").fetchone()
    assert tuple(event)==('b-1','c-1','1..open')
    row=conn.execute("SELECT processing_status,next_attempt_at,last_error FROM mirror_discord_inbound_state WHERE discord_message_id='10'").fetchone()
    assert tuple(row)==('pending',101,None)


def test_recovery_dispositions_terminal_history_instead_of_replaying_it(tmp_path):
    conn=connect_mirror(tmp_path/'m.db')
    ing=DiscordBackfillIngestor(conn,clock=lambda:100)
    import asyncio
    asyncio.run(ing.ingest_live(DiscordInbound('10','1','approved',created_at=50)))
    bind(conn,'1')

    result=recover_pending_inbound_bindings(
        conn, cards={'c-1':('done','1970-01-01T00:01:00+00:00')}, now=101,
    )

    assert result=={'rebound':0,'superseded':1,'deduplicated':0}
    row=conn.execute("SELECT processing_status,processed_at FROM mirror_discord_inbound_state WHERE discord_message_id='10'").fetchone()
    assert tuple(row)==('processed',101)
    disposition=conn.execute("SELECT disposition FROM mirror_discord_inbound_dispositions WHERE discord_message_id='10'").fetchone()[0]
    assert disposition=='superseded_by_terminal_completion'


def test_recovery_leaves_quarantined_or_temporally_ambiguous_events_pending(tmp_path):
    conn=connect_mirror(tmp_path/'m.db')
    ing=DiscordBackfillIngestor(conn,clock=lambda:100)
    import asyncio
    asyncio.run(ing.ingest_live(DiscordInbound('10','1','x',created_at=50)))
    bind(conn,'1')
    conn.execute("INSERT INTO mirror_thread_quarantine(thread_id,needs_repair,quarantined_at,updated_at) VALUES('1',1,1,1)")
    conn.commit()

    assert recover_pending_inbound_bindings(conn,cards={'c-1':('running',None)},now=101)=={'rebound':0,'superseded':0,'deduplicated':0}
    assert conn.execute("SELECT binding_key FROM mirror_conversation_events WHERE discord_message_id='10'").fetchone()[0] is None


def test_recovery_deduplicates_existing_legacy_receipt_without_dispatch(tmp_path):
    conn=connect_mirror(tmp_path/'m.db')
    ing=DiscordBackfillIngestor(conn,clock=lambda:100)
    import asyncio
    asyncio.run(ing.ingest_live(DiscordInbound('10','1','already logged',created_at=50)))
    bind(conn,'1')
    conn.execute("""INSERT INTO mirror_inbox_receipts
        (discord_message_id,board_slug,forum_channel_id,thread_id,task_id,author_id,
         action,kanban_comment_id,created_at)
        VALUES ('10','x','forum','1','c-1','user','comment',680,60)""")
    conn.commit()

    result=recover_pending_inbound_bindings(
        conn,cards={'c-1':('blocked',None)},now=101,
    )

    assert result=={'rebound':0,'superseded':0,'deduplicated':1}
    row=conn.execute("SELECT processing_status FROM mirror_discord_inbound_state WHERE discord_message_id='10'").fetchone()
    assert row[0]=='processed'
    disposition=conn.execute("SELECT disposition FROM mirror_discord_inbound_dispositions WHERE discord_message_id='10'").fetchone()[0]
    assert disposition=='already_recorded_legacy'
