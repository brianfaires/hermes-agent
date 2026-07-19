from types import SimpleNamespace
import pytest
from gateway.kanban_mirror.outbox import OutboundEnvelope, enqueue, get
from gateway.kanban_mirror.recovery import delivery_health, run_outbound_recovery
from gateway.kanban_mirror.state import connect_mirror

@pytest.fixture
def conn(tmp_path):
    db=connect_mirror(tmp_path/'mirror.db'); yield db; db.close()

def add(conn, profile='ops', thread='t1', correlation='c1'):
    op=enqueue(conn,OutboundEnvelope(profile,thread,None,'frozen',(),correlation))
    conn.execute("UPDATE mirror_discord_outbox SET created_at=100,updated_at=100 WHERE operation_id=?",(op,)); conn.commit()
    return op

@pytest.mark.asyncio
async def test_outage_backoff_expired_lease_and_health(conn):
    op=add(conn); clock=lambda:1000
    result=await run_outbound_recovery(conn,worker_id='w',adapters={},send=None,transition_publishers={},clock=clock,base_backoff=10)
    assert result['failed']==1
    row=get(conn,op); assert row['attempt_count']==1 and row['next_attempt_at']==1010 and 'disconnected' in row['last_error']
    health=delivery_health(conn,adapters={},now=1001)
    assert (health.failed_count,health.oldest_age_seconds,health.next_due_at,health.profile_availability)==(1,901,1010,{'ops':False})
    conn.execute("UPDATE mirror_discord_outbox SET status='pending',next_attempt_at=0,lease_owner='dead',lease_expires_at=999 WHERE operation_id=?",(op,)); conn.commit()
    async def send(adapter,payload): return SimpleNamespace(success=True,message_id='m1')
    result=await run_outbound_recovery(conn,worker_id='new',adapters={'ops':SimpleNamespace(is_connected=True)},send=send,transition_publishers={},clock=clock)
    assert result['delivered']==1 and get(conn,op)['discord_message_id']=='m1'

@pytest.mark.asyncio
async def test_uncertain_send_is_not_retried(conn):
    op=add(conn); calls=[]
    async def send(adapter,payload): calls.append(payload); return SimpleNamespace(success=True,message_id=None)
    kw=dict(worker_id='w',adapters={'ops':SimpleNamespace(is_connected=True)},send=send,transition_publishers={},clock=lambda:100)
    assert (await run_outbound_recovery(conn,**kw))['confirmation_needed']==1
    assert (await run_outbound_recovery(conn,**kw))['claimed']==0
    assert len(calls)==1 and get(conn,op)['status']=='confirmation_needed'

@pytest.mark.asyncio
async def test_fairness_isolation_and_frozen_identity_quarantine(conn):
    bad=add(conn,'bad','same','a'); later=add(conn,'bad','same','b'); good=add(conn,'good','other','c')
    conn.execute("UPDATE mirror_discord_outbox SET payload_hash='tampered' WHERE operation_id=?",(bad,)); conn.commit()
    async def send(adapter,payload): return SimpleNamespace(success=True,message_id='ok')
    result=await run_outbound_recovery(conn,worker_id='w',adapters={'bad':SimpleNamespace(is_connected=True),'good':SimpleNamespace(is_connected=True)},send=send,transition_publishers={},clock=lambda:200,limit=10)
    assert result['claimed']==2 and result['quarantined']==1 and result['delivered']==1
    assert get(conn,bad)['status']=='quarantined' and get(conn,later)['status']=='pending'
    assert conn.execute("SELECT code FROM mirror_reconciliation_findings").fetchone()[0]=='delivery.poison'

@pytest.mark.asyncio
async def test_live_lease_prevents_duplicate_worker(conn):
    op=add(conn); conn.execute("UPDATE mirror_discord_outbox SET lease_owner='w1',lease_expires_at=500 WHERE operation_id=?",(op,)); conn.commit(); called=[]
    async def send(a,p): called.append(1)
    result=await run_outbound_recovery(conn,worker_id='w2',adapters={'ops':SimpleNamespace(is_connected=True)},send=send,transition_publishers={},clock=lambda:100)
    assert result['claimed']==0 and not called
