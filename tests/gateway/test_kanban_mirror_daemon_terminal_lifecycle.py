import asyncio
from plugins.platforms.discord.kanban_mirror.config import MirrorConfig, load_mirror_config
from plugins.platforms.discord.kanban_mirror.daemon import _resume_terminal_lifecycles
from plugins.platforms.discord.kanban_mirror.lifecycle_discord import DiscordLifecyclePublisher
from plugins.platforms.discord.kanban_mirror.state import (BoardSnapshot, Card, add_member, backfill_legacy_bindings,
    connect_mirror, create_initiative, load_mirror_state, set_thread)


class Discord:
    def __init__(self):
        self.forum={"id":"forum","available_tags":[{"id":"done-id","name":"done"}]}
        self.channels={"thread":{"id":"thread","applied_tags":[],"last_message_id":"last","pinned":False,"archived":False},
                       "digest":{"id":"digest","applied_tags":[],"last_message_id":"digest","pinned":False}}
        self.messages={("thread","last"):{"id":"last","content":"work","timestamp":"1970-01-01T00:01:30Z"},
                       ("digest","digest"):{"id":"digest","content":"Board","timestamp":"1970-01-01T00:00:01Z"}}
        self.nonces={}; self.events=[]
    def get_channel(self,c): return self.forum if c=="forum" else dict(self.channels[c])
    def get_message(self,c,m): return dict(self.messages[(c,m)])
    def send_message(self,c,*,content,nonce=None):
        self.events.append("summary"); self.nonces.setdefault(nonce,{"id":"summary","content":content}); return self.nonces[nonce]
    def update_message(self,c,m,*,content):
        self.events.append("digest"); self.messages[(c,m)]={"id":m,"content":content}; return self.messages[(c,m)]
    def update_thread(self,c,*,tag_ids=None,pinned=None,archive=None,**kw):
        if pinned is not None: self.channels[c]["pinned"]=pinned
        if tag_ids is not None: self.events.append("tag"); self.channels[c]["applied_tags"]=tag_ids
        if archive is not None: self.events.append("archive"); self.channels[c]["archived"]=archive
        return dict(self.channels[c])


def card(status="done"):
    return Card("card","Detailed title","body",status,"high","Ops",None,None,None,"1","2",None,"shipped")


def seeded(path):
    conn=connect_mirror(path)
    create_initiative(conn,"i","Work"); add_member(conn,"i","card"); set_thread(conn,"i","thread","thread")
    create_initiative(conn,"digest","Board","digest"); set_thread(conn,"digest","digest","digest")
    backfill_legacy_bindings(conn,"board")
    return conn


def snapshot(status="done"):
    return BoardSnapshot({"card":card(status)}, {}, {}, {}, {})


def test_concrete_publisher_nonce_digest_pin_tag_and_archive(tmp_path):
    conn=seeded(tmp_path/"m.db"); client=Discord(); cfg=MirrorConfig(forum_channel_id="forum")
    pub=DiscordLifecyclePublisher(client,cfg,conn)
    payload={"card_chain":[{"task_id":"card","title":"Detailed title","status":"done"}],"outcomes":[{"outcome":"shipped"}],"owners":["Ops"],"date_range":{}}
    assert pub.publish_summary("thread",payload,operation_key="stable")==pub.publish_summary("thread",payload,operation_key="stable")
    assert len(client.nonces)==1
    digest={"thread_id":"thread","outcome":"shipped","date_range":{"end":"2026-07-12"},"thread_link":"https://discord/thread"}
    pub.upsert_digest("thread",digest,operation_key="digest")
    assert client.channels["digest"]["pinned"]
    assert "Detailed title" not in client.messages[("digest","digest")]["content"]
    pub.apply_done_tag("thread",{"done":True},operation_key="tag")
    assert pub.read_thread_state("thread")["done"]
    pub.archive_thread("thread",{"archived":True},operation_key="archive")
    assert pub.read_thread_state("thread")["archived"]


def test_daemon_resume_orders_stages_and_reopen_cancels(tmp_path, monkeypatch):
    conn=seeded(tmp_path/"m.db"); client=Discord()
    cfg=MirrorConfig(board="board",forum_channel_id="forum",guild_id="guild",terminal_lifecycle_enabled=True,done_thread_archive_idle_minutes=1)
    monkeypatch.setattr("plugins.platforms.discord.kanban_mirror.daemon.time.time",lambda:100)
    state=load_mirror_state(conn); log=[]
    asyncio.run(_resume_terminal_lifecycles(cfg,client,conn,snapshot(),state,log))
    assert client.events==["summary","digest","tag"]
    # Restart resumes from durable tag boundary; later Discord activity resets idle.
    client.messages[("thread","last")]["timestamp"]="1970-01-01T00:02:50Z"
    monkeypatch.setattr("plugins.platforms.discord.kanban_mirror.daemon.time.time",lambda:200)
    asyncio.run(_resume_terminal_lifecycles(cfg,client,conn,snapshot(),state,log))
    assert "archive" not in client.events
    asyncio.run(_resume_terminal_lifecycles(cfg,client,conn,snapshot("running"),state,log))
    assert conn.execute("SELECT state FROM mirror_terminal_lifecycles").fetchone()[0]=="cancelled"


def test_feature_gate_disabled_by_default_preserves_legacy():
    assert not load_mirror_config({}).terminal_lifecycle_enabled
    assert not MirrorConfig().terminal_lifecycle_enabled
