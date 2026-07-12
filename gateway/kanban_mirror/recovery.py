"""Durable, fakeable recovery runner and outbound delivery health queries."""
from __future__ import annotations
import hashlib, json, sqlite3, time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from .outbox import ensure_outbox_schema
from .state import get_binding_transition
from .transitions import run_binding_transition

SQL = """CREATE TABLE IF NOT EXISTS mirror_transition_recovery(
 transition_key TEXT PRIMARY KEY, frozen_hash TEXT NOT NULL, thread_id TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',attempt_count INTEGER NOT NULL DEFAULT 0,last_error TEXT,
 next_attempt_at INTEGER,lease_owner TEXT,lease_expires_at INTEGER,created_at INTEGER NOT NULL,
 updated_at INTEGER NOT NULL,quarantined_at INTEGER);
CREATE INDEX IF NOT EXISTS idx_transition_recovery_due ON mirror_transition_recovery(status,next_attempt_at);"""

@dataclass(frozen=True)
class DeliveryHealth:
    pending_count: int; failed_count: int; oldest_age_seconds: int | None
    next_due_at: int | None; lease_count: int; profile_availability: dict[str,bool]

def ensure_schema(conn: sqlite3.Connection, now: int) -> None:
    ensure_outbox_schema(conn); conn.executescript(SQL)
    conn.execute("""INSERT OR IGNORE INTO mirror_transition_recovery
      SELECT transition_key,frozen_hash,thread_id,'pending',0,NULL,NULL,NULL,NULL,prepared_at,prepared_at,NULL
      FROM mirror_binding_transitions WHERE state!='starter_verified'"""); conn.commit()

def _finding(conn, op, thread, error, now):
    evidence=json.dumps({'operation_id':op,'error':error},sort_keys=True,separators=(',',':'))
    key=hashlib.sha256(f'delivery.poison\0{thread}\0{op}'.encode()).hexdigest()
    conn.execute("""INSERT INTO mirror_reconciliation_findings
      (finding_key,severity,code,thread_id,evidence,evidence_hash,first_seen_at,last_seen_at)
      VALUES (?,'error','delivery.poison',?,?,?,?,?) ON CONFLICT(finding_key) DO UPDATE SET
      evidence=excluded.evidence,evidence_hash=excluded.evidence_hash,last_seen_at=excluded.last_seen_at,resolved_at=NULL""",
      (key,thread,evidence,hashlib.sha256(evidence.encode()).hexdigest(),now,now))

def _claim(conn, worker, now, lease, limit):
    conn.execute('BEGIN IMMEDIATE')
    try:
        rows=conn.execute("""SELECT 'discord' kind,operation_id op,target_profile profile,thread_id,created_at FROM mirror_discord_outbox
          WHERE status IN ('pending','failed') AND confirmation_needed_at IS NULL AND quarantined_at IS NULL
          AND (next_attempt_at IS NULL OR next_attempt_at<=?) AND COALESCE(lease_expires_at,0)<=?
          UNION ALL SELECT 'transition',transition_key,NULL,thread_id,created_at FROM mirror_transition_recovery
          WHERE status IN ('pending','failed') AND quarantined_at IS NULL AND (next_attempt_at IS NULL OR next_attempt_at<=?)
          AND COALESCE(lease_expires_at,0)<=? ORDER BY created_at,op""",(now,now,now,now)).fetchall()
        picked=[]; domains=set()
        for r in rows:
            domain=(r['profile'] or 'controller',r['thread_id'])
            if domain in domains: continue
            domains.add(domain); picked.append(r)
            if len(picked)>=limit: break
        for r in picked:
            table='mirror_discord_outbox' if r['kind']=='discord' else 'mirror_transition_recovery'
            key='operation_id' if r['kind']=='discord' else 'transition_key'
            conn.execute(f'UPDATE {table} SET lease_owner=?,lease_expires_at=?,updated_at=? WHERE {key}=?',(worker,now+lease,now,r['op']))
        conn.commit(); return picked
    except Exception: conn.rollback(); raise

async def run_outbound_recovery(conn: sqlite3.Connection, *, worker_id: str,
 adapters: Mapping[str,Any], send: Callable[[Any,dict],Awaitable[Any]],
 transition_publishers: Mapping[str,Any], clock: Callable[[],float]=time.time,
 limit: int=20, lease_seconds: int=300, base_backoff: int=5, max_backoff: int=3600):
    now=int(clock()); ensure_schema(conn,now); items=_claim(conn,worker_id,now,lease_seconds,limit)
    stats={'claimed':len(items),'delivered':0,'failed':0,'confirmation_needed':0,'quarantined':0}
    for item in items:
      table='mirror_discord_outbox' if item['kind']=='discord' else 'mirror_transition_recovery'; key='operation_id' if item['kind']=='discord' else 'transition_key'
      try:
        if item['kind']=='discord':
          row=conn.execute('SELECT * FROM mirror_discord_outbox WHERE operation_id=?',(item['op'],)).fetchone(); payload=json.loads(row['payload'])
          immutable=hashlib.sha256(row['payload'].encode()).hexdigest()==row['payload_hash'] and payload.get('profile')==row['target_profile'] and str(payload.get('thread_id'))==row['thread_id']
          if not immutable: raise ValueError('immutable outbox identity mismatch')
          adapter=adapters.get(row['target_profile'])
          if adapter is None or not getattr(adapter,'is_connected',False): raise RuntimeError('target profile adapter is missing or disconnected')
          try:
            reply=await send(adapter,payload)
          except Exception as exc:
            # Once the adapter call begins, a timeout/disconnect cannot prove
            # Discord did not accept the message.  Stop automatic retries.
            conn.execute("UPDATE mirror_discord_outbox SET status='confirmation_needed',attempt_count=attempt_count+1,last_error=?,confirmation_needed_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE operation_id=? AND lease_owner=?",(str(exc),now,now,item['op'],worker_id)); stats['confirmation_needed']+=1
            conn.commit(); continue
          mid=getattr(reply,'message_id',None)
          if getattr(reply,'success',False) and mid:
            conn.execute("UPDATE mirror_discord_outbox SET status='delivered',attempt_count=attempt_count+1,last_error=NULL,discord_message_id=?,delivered_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE operation_id=? AND lease_owner=?",(str(mid),now,now,item['op'],worker_id)); stats['delivered']+=1
          elif getattr(reply,'success',False):
            conn.execute("UPDATE mirror_discord_outbox SET status='confirmation_needed',attempt_count=attempt_count+1,last_error='send outcome requires confirmation',confirmation_needed_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE operation_id=? AND lease_owner=?",(now,now,item['op'],worker_id)); stats['confirmation_needed']+=1
          else: raise RuntimeError(getattr(reply,'error',None) or 'Discord send failed')
        else:
          meta=conn.execute('SELECT * FROM mirror_transition_recovery WHERE transition_key=?',(item['op'],)).fetchone(); t=get_binding_transition(conn,item['op'])
          if t is None or t.frozen_hash!=meta['frozen_hash']: raise ValueError('immutable transition identity mismatch')
          pub=transition_publishers.get(t.thread_id)
          if pub is None: raise RuntimeError('transition publisher unavailable')
          done=run_binding_transition(conn,pub,transition_key=t.transition_key,thread_id=t.thread_id,old_card_metadata=t.old_card_metadata,new_card_metadata=t.new_card_metadata,transition_payload=t.transition_payload,starter_payload=t.starter_payload)
          if done.state!='starter_verified': raise RuntimeError('transition was not confirmed')
          conn.execute("UPDATE mirror_transition_recovery SET status='delivered',attempt_count=attempt_count+1,last_error=NULL,updated_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE transition_key=? AND lease_owner=?",(now,item['op'],worker_id)); stats['delivered']+=1
      except Exception as exc:
        fatal=isinstance(exc,ValueError) and 'immutable' in str(exc); attempt=conn.execute(f'SELECT attempt_count FROM {table} WHERE {key}=?',(item['op'],)).fetchone()[0]+1
        if fatal:
          conn.execute(f"UPDATE {table} SET status='quarantined',attempt_count=?,last_error=?,quarantined_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE {key}=? AND lease_owner=?",(attempt,str(exc),now,now,item['op'],worker_id)); _finding(conn,item['op'],item['thread_id'],str(exc),now); stats['quarantined']+=1
        else:
          due=now+min(max_backoff,base_backoff*2**max(0,attempt-1)); conn.execute(f"UPDATE {table} SET status='failed',attempt_count=?,last_error=?,next_attempt_at=?,updated_at=?,lease_owner=NULL,lease_expires_at=NULL WHERE {key}=? AND lease_owner=?",(attempt,str(exc),due,now,item['op'],worker_id)); stats['failed']+=1
      conn.commit()
    return stats

def delivery_health(conn: sqlite3.Connection, *, adapters: Mapping[str,Any], now: int|None=None) -> DeliveryHealth:
    stamp=int(time.time()) if now is None else int(now); ensure_schema(conn,stamp)
    rows=conn.execute("""SELECT status,created_at,next_attempt_at,lease_expires_at,target_profile profile FROM mirror_discord_outbox WHERE status!='delivered'
      UNION ALL SELECT status,created_at,next_attempt_at,lease_expires_at,NULL FROM mirror_transition_recovery WHERE status!='delivered'""").fetchall()
    active=[r for r in rows if r['status'] not in ('quarantined','confirmation_needed')]; oldest=min((r['created_at'] for r in active),default=None)
    profiles=sorted({r['profile'] for r in rows if r['profile']})
    return DeliveryHealth(sum(r['status']=='pending' for r in active),sum(r['status']=='failed' for r in active),None if oldest is None else max(0,stamp-oldest),min((r['next_attempt_at'] or r['created_at'] for r in active),default=None),sum(bool(r['lease_expires_at'] and r['lease_expires_at']>stamp) for r in rows),{p:bool(adapters.get(p) and getattr(adapters[p],'is_connected',False)) for p in profiles})
