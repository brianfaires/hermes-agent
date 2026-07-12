"""Unwired, durable terminal digest and idle archival orchestration foundation."""
from __future__ import annotations
import hashlib, json, sqlite3
from dataclasses import dataclass
from typing import Callable, Protocol
from .state import active_thread_binding, is_terminal, is_thread_quarantined

def _json(v): return json.dumps(v, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
def _hash(v): return hashlib.sha256(_json(v).encode()).hexdigest()

@dataclass(frozen=True)
class PublishReceipt:
    operation_key: str; thread_id: str; payload_hash: str; object_id: str
@dataclass(frozen=True)
class TerminalLifecycle:
    lifecycle_key: str; thread_id: str; binding_key: str; frozen_payload: dict
    frozen_hash: str; state: str; summary_message_id: str | None
    digest_entry_id: str | None; latest_activity_at: int; archive_due_at: int | None
    last_error: str | None
class LifecyclePublisher(Protocol):
    def publish_summary(self, thread_id: str, payload: dict, *, operation_key: str) -> PublishReceipt: ...
    def upsert_digest(self, thread_id: str, payload: dict, *, operation_key: str) -> PublishReceipt: ...
    def apply_done_tag(self, thread_id: str, payload: dict, *, operation_key: str) -> PublishReceipt: ...
    def archive_thread(self, thread_id: str, payload: dict, *, operation_key: str) -> PublishReceipt: ...
    def read_thread_state(self, thread_id: str) -> dict: ...

def _from(row):
    return TerminalLifecycle(row["lifecycle_key"],row["thread_id"],row["binding_key"],json.loads(row["frozen_payload"]),row["frozen_hash"],row["state"],row["summary_message_id"],row["digest_entry_id"],row["latest_activity_at"],row["archive_due_at"],row["last_error"])
def get_terminal_lifecycle(conn, key):
    row=conn.execute("SELECT * FROM mirror_terminal_lifecycles WHERE lifecycle_key=?",(key,)).fetchone()
    return _from(row) if row else None

def _pending(conn, thread):
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    checks=(("mirror_binding_transitions","thread_id=? AND state!='starter_verified'"),("mirror_conversation_deliveries","thread_id=? AND status!='delivered'"),("mirror_discord_outbox","thread_id=? AND status!='delivered'"),("mirror_discord_inbound_state","thread_id=? AND processing_status!='processed'"))
    return any(t in tables and conn.execute(f"SELECT 1 FROM {t} WHERE {w} LIMIT 1",(thread,)).fetchone() for t,w in checks)
def _activity(conn,thread,observed):
    row=conn.execute("SELECT MAX(COALESCE(discord_created_at,recorded_at)) FROM mirror_conversation_events WHERE thread_id=? AND (event_class LIKE 'conversation.%' OR event_class='directive.user')",(thread,)).fetchone()
    return max(int(observed),int(row[0] or 0))
def cancel_pending_archive(conn,thread_id,*,now):
    conn.execute("UPDATE mirror_terminal_lifecycles SET state='cancelled',archive_due_at=NULL,updated_at=? WHERE thread_id=? AND state NOT IN ('archived','cancelled')",(int(now),str(thread_id)));conn.commit()
def _receipt(r,key,thread,payload):
    if not isinstance(r,PublishReceipt) or (r.operation_key,r.thread_id,r.payload_hash)!=(key,thread,_hash(payload)) or not r.object_id.strip():
        raise ValueError("publisher receipt does not match frozen operation")

def run_terminal_lifecycle(conn: sqlite3.Connection,publisher: LifecyclePublisher,*,lifecycle_key:str,thread_id:str,card_chain:list[dict],outcomes:list[dict],owners:list[str],date_range:dict,thread_link:str,idle_seconds:int,observed_activity_at:int,clock:Callable[[],int]):
    """Resume strict summary -> digest -> tag -> idle -> archive boundaries."""
    now=int(clock());thread=str(thread_id);binding=active_thread_binding(conn,thread)
    terminal=bool(binding and card_chain and str(card_chain[-1].get("task_id"))==binding.task_id and all(is_terminal(str(c.get("status",""))) for c in card_chain))
    if not terminal:
        cancel_pending_archive(conn,thread,now=now);return None
    if is_thread_quarantined(conn,thread) or _pending(conn,thread): return None
    summary={"thread_id":thread,"binding_key":binding.binding_key,"card_chain":card_chain,"outcomes":outcomes,"date_range":date_range,"owners":owners,"thread_link":thread_link}
    digest={"thread_id":thread,"outcome":outcomes[-1].get("outcome") if outcomes else "completed","date_range":date_range,"thread_link":thread_link}
    frozen={"summary":summary,"digest":digest}; raw=_json(frozen); fh=_hash(frozen); activity=_activity(conn,thread,observed_activity_at)
    current=conn.execute("SELECT lifecycle_key FROM mirror_terminal_lifecycles WHERE thread_id=? AND state NOT IN ('archived','cancelled')",(thread,)).fetchone()
    if current is not None and current["lifecycle_key"]!=lifecycle_key:
        raise ValueError("thread already has a different active terminal lifecycle")
    conn.execute("INSERT OR IGNORE INTO mirror_terminal_lifecycles(lifecycle_key,thread_id,binding_key,frozen_payload,frozen_hash,state,latest_activity_at,prepared_at,updated_at) VALUES(?,?,?,?,?,'prepared',?,?,?)",(lifecycle_key,thread,binding.binding_key,raw,fh,activity,now,now));conn.commit()
    life=get_terminal_lifecycle(conn,lifecycle_key)
    if life is None or (life.thread_id,life.binding_key)!=(thread,binding.binding_key):
        raise ValueError("lifecycle retry does not match frozen state")
    if activity>life.latest_activity_at:
        conn.execute("UPDATE mirror_terminal_lifecycles SET latest_activity_at=?,archive_due_at=?,updated_at=? WHERE lifecycle_key=?",(activity,activity+int(idle_seconds),now,lifecycle_key));conn.commit();life=get_terminal_lifecycle(conn,lifecycle_key)
    try:
        if life.state=="prepared":
            p=life.frozen_payload["summary"];key=lifecycle_key+":summary";r=publisher.publish_summary(thread,p,operation_key=key);_receipt(r,key,thread,p)
            conn.execute("UPDATE mirror_terminal_lifecycles SET state='summary_confirmed',summary_message_id=?,summary_confirmed_at=?,last_error=NULL,updated_at=? WHERE lifecycle_key=?",(r.object_id,now,now,lifecycle_key));conn.commit();life=get_terminal_lifecycle(conn,lifecycle_key)
        if life.state=="summary_confirmed":
            p=life.frozen_payload["digest"];key=lifecycle_key+":digest";r=publisher.upsert_digest(thread,p,operation_key=key);_receipt(r,key,thread,p)
            conn.execute("UPDATE mirror_terminal_lifecycles SET state='digest_confirmed',digest_entry_id=?,digest_confirmed_at=?,last_error=NULL,updated_at=? WHERE lifecycle_key=?",(r.object_id,now,now,lifecycle_key));conn.commit();life=get_terminal_lifecycle(conn,lifecycle_key)
        if life.state=="digest_confirmed":
            p={"done":True};key=lifecycle_key+":tag";r=publisher.apply_done_tag(thread,p,operation_key=key);_receipt(r,key,thread,p);live=publisher.read_thread_state(thread)
            if not isinstance(live,dict) or not live.get("done") or live.get("archived"): raise ValueError("live thread does not confirm done tag")
            conn.execute("UPDATE mirror_terminal_lifecycles SET state='tag_confirmed',tag_confirmed_at=?,archive_due_at=?,last_error=NULL,updated_at=? WHERE lifecycle_key=?",(now,activity+int(idle_seconds),now,lifecycle_key));conn.commit();life=get_terminal_lifecycle(conn,lifecycle_key)
        if life.state=="tag_confirmed" and now>=life.archive_due_at:
            # Re-read immediately before the irreversible side effect. Events
            # may have landed while summary/digest/tag publishers were called.
            live_before=publisher.read_thread_state(thread)
            live_activity=int(live_before.get("latest_activity_at") or 0) if isinstance(live_before,dict) else 0
            latest=_activity(conn,thread,max(observed_activity_at,live_activity))
            if latest>life.latest_activity_at:
                conn.execute("UPDATE mirror_terminal_lifecycles SET latest_activity_at=?,archive_due_at=?,updated_at=? WHERE lifecycle_key=?",(latest,latest+int(idle_seconds),now,lifecycle_key));conn.commit();return get_terminal_lifecycle(conn,lifecycle_key)
            if is_thread_quarantined(conn,thread) or _pending(conn,thread) or not live_before.get("done"): return life
            if live_before.get("archived"):
                # Recover a crash after Discord accepted the archive but before
                # the local confirmation commit.
                conn.execute("UPDATE mirror_terminal_lifecycles SET state='archived',archived_at=?,last_error=NULL,updated_at=? WHERE lifecycle_key=?",(now,now,lifecycle_key));conn.commit();return get_terminal_lifecycle(conn,lifecycle_key)
            p={"archived":True};key=lifecycle_key+":archive";r=publisher.archive_thread(thread,p,operation_key=key);_receipt(r,key,thread,p);live=publisher.read_thread_state(thread)
            if not isinstance(live,dict) or not live.get("done") or not live.get("archived"): raise ValueError("live thread does not confirm archive")
            conn.execute("UPDATE mirror_terminal_lifecycles SET state='archived',archived_at=?,last_error=NULL,updated_at=? WHERE lifecycle_key=?",(now,now,lifecycle_key));conn.commit();life=get_terminal_lifecycle(conn,lifecycle_key)
    except Exception as exc:
        conn.execute("UPDATE mirror_terminal_lifecycles SET last_error=?,updated_at=? WHERE lifecycle_key=?",(str(exc),now,lifecycle_key));conn.commit();raise
    return life
