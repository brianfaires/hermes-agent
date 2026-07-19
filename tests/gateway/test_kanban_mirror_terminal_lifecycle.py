import hashlib,json
import pytest
from gateway.kanban_mirror.state import connect_mirror,create_initiative,add_member,set_thread,backfill_legacy_bindings
from gateway.kanban_mirror.lifecycle import PublishReceipt,get_terminal_lifecycle,run_terminal_lifecycle

def h(p): return hashlib.sha256(json.dumps(p,ensure_ascii=False,separators=(",",":"),sort_keys=True).encode()).hexdigest()
class Clock:
 def __init__(self,n=100):self.n=n
 def __call__(self):return self.n
class Pub:
 def __init__(self):self.calls=[];self.receipts={};self.live={"done":False,"archived":False};self.fail=None;self.bad=False
 def op(self,name,t,p,key):
  self.calls.append((name,key,p))
  if self.fail==name:raise RuntimeError(name+" failed")
  r=self.receipts.setdefault(key,PublishReceipt(key,t,h(p),name+"-id"))
  return PublishReceipt(key,t,"bad",r.object_id) if self.bad else r
 def publish_summary(self,t,p,*,operation_key):return self.op("summary",t,p,operation_key)
 def upsert_digest(self,t,p,*,operation_key):return self.op("digest",t,p,operation_key)
 def apply_done_tag(self,t,p,*,operation_key):
  r=self.op("tag",t,p,operation_key);self.live["done"]=True;return r
 def archive_thread(self,t,p,*,operation_key):
  r=self.op("archive",t,p,operation_key);self.live["archived"]=True;return r
 def read_thread_state(self,t):return dict(self.live)
def seed(path):
 c=connect_mirror(path);create_initiative(c,"i","T");add_member(c,"i","card");set_thread(c,"i","thread","starter");backfill_legacy_bindings(c,"board");return c
def args(clock):return dict(lifecycle_key="terminal:thread:1",thread_id="thread",card_chain=[{"task_id":"card","title":"Full detail","status":"done"}],outcomes=[{"task_id":"card","outcome":"shipped"}],owners=["Ops"],date_range={"start":"2026-01-01","end":"2026-01-02"},thread_link="https://discord/thread",idle_seconds=50,observed_activity_at=90,clock=clock)

def test_orders_confirmations_and_archives_only_after_idle(tmp_path):
 c=seed(tmp_path/"m.db");p=Pub();clock=Clock();life=run_terminal_lifecycle(c,p,**args(clock))
 assert life.state=="tag_confirmed";assert [x[0] for x in p.calls]==["summary","digest","tag"]
 assert set(life.frozen_payload)=={"summary","digest"};assert "card_chain" not in life.frozen_payload["digest"]
 clock.n=140;assert run_terminal_lifecycle(c,p,**args(clock)).state=="archived"
 assert [x[0] for x in p.calls]==["summary","digest","tag","archive"]

def test_retry_each_boundary_is_idempotent_and_pending_error_queryable(tmp_path):
 for failed,expected in [("summary","prepared"),("digest","summary_confirmed"),("tag","digest_confirmed"),("archive","tag_confirmed")]:
  c=seed(tmp_path/(failed+".db"));p=Pub();clock=Clock(200);p.fail=failed
  with pytest.raises(RuntimeError):run_terminal_lifecycle(c,p,**args(clock))
  life=get_terminal_lifecycle(c,"terminal:thread:1");assert life.state==expected;assert failed in life.last_error
  p.fail=None;assert run_terminal_lifecycle(c,p,**args(clock)).state=="archived"
  assert len(p.receipts)==4

def test_activity_resets_idle_and_reopen_cancels(tmp_path):
 c=seed(tmp_path/"m.db");p=Pub();clock=Clock();run_terminal_lifecycle(c,p,**args(clock))
 c.execute("INSERT INTO mirror_conversation_events(discord_message_id,thread_id,binding_key,event_class,author_label,content,discord_created_at,recorded_at) VALUES('human','thread',NULL,'conversation.human','u','new',130,130)");c.commit()
 clock.n=140;life=run_terminal_lifecycle(c,p,**args(clock));assert life.archive_due_at==180;assert life.state=="tag_confirmed"
 reopened=args(clock);reopened["card_chain"]=[{"task_id":"card","status":"running"}]
 assert run_terminal_lifecycle(c,p,**reopened) is None;assert get_terminal_lifecycle(c,"terminal:thread:1").state=="cancelled"

def test_quarantine_and_pending_work_fail_closed(tmp_path):
 c=seed(tmp_path/"q.db");p=Pub();clock=Clock(200)
 c.execute("INSERT INTO mirror_thread_quarantine(thread_id,quarantined_at,updated_at) VALUES('thread',1,1)");c.commit()
 assert run_terminal_lifecycle(c,p,**args(clock)) is None;assert not p.calls
 c=seed(tmp_path/"pending.db");c.execute("INSERT INTO mirror_conversation_deliveries(operation_id,trigger_discord_message_id,thread_id,task_id,mode,payload,payload_hash,status,created_at,updated_at) VALUES('o','m','thread','card','x','{}','h','pending',1,1)");c.commit()
 assert run_terminal_lifecycle(c,p,**args(clock)) is None;assert not p.calls

def test_mismatched_receipt_leaves_frozen_pending_state(tmp_path):
 c=seed(tmp_path/"m.db");p=Pub();p.bad=True
 with pytest.raises(ValueError,match="receipt"):run_terminal_lifecycle(c,p,**args(Clock()))
 life=get_terminal_lifecycle(c,"terminal:thread:1");assert life.state=="prepared";assert life.last_error


def test_retry_uses_frozen_summary_despite_late_metadata_drift(tmp_path):
 c=seed(tmp_path/"m.db");p=Pub();p.fail="summary";clock=Clock()
 original=args(clock)
 with pytest.raises(RuntimeError):run_terminal_lifecycle(c,p,**original)
 changed=args(clock);changed["outcomes"]=[{"task_id":"card","outcome":"late edit"}]
 p.fail=None;run_terminal_lifecycle(c,p,**changed)
 assert [x for x in p.calls if x[0]=="summary"][-1][2]["outcomes"]==original["outcomes"]
 changed["lifecycle_key"]="different"
 with pytest.raises(ValueError,match="different active"):run_terminal_lifecycle(c,p,**changed)
