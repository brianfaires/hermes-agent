import asyncio
from plugins.platforms.discord.kanban_mirror.config import MirrorConfig, load_mirror_config
from plugins.platforms.discord.kanban_mirror.daemon import _resume_terminal_lifecycles
from plugins.platforms.discord.kanban_mirror.lifecycle_discord import DiscordLifecyclePublisher, _bounded_digest_content
from plugins.platforms.discord.kanban_mirror.state import (BoardSnapshot, Card, add_member, backfill_legacy_bindings,
    connect_mirror, create_initiative, load_mirror_state, set_archived, set_thread)


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
        if archive is not None:
            self.events.append("archive")
            self.channels[c]["archived"]=archive
            self.channels[c].setdefault("thread_metadata", {})["archived"]=archive
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


def test_done_tag_replaces_stale_lifecycle_tags_but_preserves_owner(tmp_path):
    conn = seeded(tmp_path / "m.db")
    client = Discord()
    client.forum["available_tags"] = [
        {"id": "done-id", "name": "done"},
        {"id": "waiting-id", "name": "waiting"},
        {"id": "needs-brian-id", "name": "needs-brian"},
        {"id": "ops-id", "name": "ops"},
    ]
    client.channels["thread"]["applied_tags"] = [
        "waiting-id", "needs-brian-id", "ops-id",
    ]
    pub = DiscordLifecyclePublisher(client, MirrorConfig(forum_channel_id="forum"), conn)

    pub.apply_done_tag("thread", {"done": True}, operation_key="tag")

    assert client.channels["thread"]["applied_tags"] == ["ops-id", "done-id"]


def test_terminal_digest_rolls_oldest_entries_under_discord_limit():
    old = "Board\n\n" + "\n\n".join(
        f"<!-- terminal:thread-{i} -->\n- [2026-07-25](https://discord/thread-{i}) — " + ("x" * 90)
        for i in range(30)
    )
    marker = "<!-- terminal:new -->"
    block = marker + "\n- [2026-07-25](https://discord/new) — shipped"
    content = _bounded_digest_content(old, marker, block)
    assert len(content) <= 2000 and block in content
    assert "<!-- terminal:thread-0 -->" not in content
    assert "<!-- terminal:thread-29 -->" in content
    assert _bounded_digest_content(content, marker, block).count(marker) == 1


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


def test_daemon_backfills_active_terminal_thread_marked_archived_in_legacy_state(tmp_path, monkeypatch):
    conn=seeded(tmp_path/"m.db"); client=Discord(); set_archived(conn,"i",80)
    cfg=MirrorConfig(board="board",forum_channel_id="forum",guild_id="guild",terminal_lifecycle_enabled=True,done_thread_archive_idle_minutes=1)
    monkeypatch.setattr("plugins.platforms.discord.kanban_mirror.daemon.time.time",lambda:100)
    log=[]
    asyncio.run(_resume_terminal_lifecycles(cfg,client,conn,snapshot(),load_mirror_state(conn),log))
    assert "terminal_lifecycle: BACKFILLED active legacy thread i" in log
    assert conn.execute("SELECT archived_at FROM mirror_initiatives WHERE id='i'").fetchone()[0] is None
    assert conn.execute("SELECT state FROM mirror_terminal_lifecycles").fetchone()[0]=="tag_confirmed"


def test_daemon_rearchives_idle_orphaned_legacy_thread_without_a_card_mapping(
    tmp_path, monkeypatch,
):
    conn=connect_mirror(tmp_path/"m.db"); client=Discord()
    create_initiative(conn,"orphan","Old completed test")
    set_thread(conn,"orphan","thread","thread")
    set_archived(conn,"orphan",80)
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,evidence,evidence_hash,
         first_seen_at,last_seen_at,resolved_at)
        VALUES ('open-count','critical','binding.open_count','thread','{}','h',90,90,NULL)""")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at,resolved_at)
        VALUES ('thread',1,90,90,NULL)""")
    conn.commit()
    cfg=MirrorConfig(board="board",forum_channel_id="forum",guild_id="guild",
        reconciliation_enabled=True,terminal_lifecycle_enabled=True,
        done_thread_archive_idle_minutes=1)
    monkeypatch.setattr("plugins.platforms.discord.kanban_mirror.daemon.time.time",lambda:200)
    log=[]

    asyncio.run(_resume_terminal_lifecycles(
        cfg,client,conn,BoardSnapshot({}, {}, {}, {}, {}),load_mirror_state(conn),log,
    ))

    assert client.channels["thread"]["archived"] is True
    assert conn.execute(
        "SELECT archived_at FROM mirror_initiatives WHERE id='orphan'"
    ).fetchone()[0] == 80
    assert conn.execute(
        "SELECT resolved_at FROM mirror_thread_quarantine WHERE thread_id='thread'"
    ).fetchone()[0] is not None
    assert "terminal_lifecycle: REARCHIVED orphaned legacy thread orphan" in log


def test_daemon_does_not_treat_quarantine_hidden_open_binding_as_an_orphan(
    tmp_path, monkeypatch,
):
    conn=connect_mirror(tmp_path/"m.db"); client=Discord()
    create_initiative(conn,"mapped","Mapped legacy thread")
    set_thread(conn,"mapped","thread","thread")
    set_archived(conn,"mapped",80)
    conn.execute("""INSERT INTO mirror_binding_epochs
        (binding_key,thread_id,board_slug,task_id,sequence,started_at,state)
        VALUES ('binding','thread','board','card',1,1,'open')""")
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,evidence,evidence_hash,
         first_seen_at,last_seen_at,resolved_at)
        VALUES ('open-count','critical','binding.open_count','thread','{}','h',90,90,NULL)""")
    conn.execute("""INSERT INTO mirror_thread_quarantine
        (thread_id,needs_repair,quarantined_at,updated_at,resolved_at)
        VALUES ('thread',1,90,90,NULL)""")
    conn.commit()
    cfg=MirrorConfig(board="board",forum_channel_id="forum",guild_id="guild",
        reconciliation_enabled=True,terminal_lifecycle_enabled=True,
        done_thread_archive_idle_minutes=1)
    monkeypatch.setattr("plugins.platforms.discord.kanban_mirror.daemon.time.time",lambda:200)

    asyncio.run(_resume_terminal_lifecycles(
        cfg,client,conn,BoardSnapshot({}, {}, {}, {}, {}),load_mirror_state(conn),[],
    ))

    assert client.channels["thread"]["archived"] is False
    assert conn.execute(
        "SELECT resolved_at FROM mirror_thread_quarantine WHERE thread_id='thread'"
    ).fetchone()[0] is None


def _seed_two_terminal_threads(conn, client):
    create_initiative(conn,"i2","Work 2"); add_member(conn,"i2","card2"); set_thread(conn,"i2","thread2","thread2")
    backfill_legacy_bindings(conn,"board")
    client.channels["thread2"]={"id":"thread2","applied_tags":[],"last_message_id":"last2","pinned":False,"archived":False}
    client.messages[("thread2","last2")]={"id":"last2","content":"work","timestamp":"1970-01-01T00:01:30Z"}
    return BoardSnapshot({
        "card":card(),
        "card2":Card("card2","Second","body","done","high","Ops",None,None,None,"1","2",None,"shipped"),
    },{}, {}, {}, {})


def test_daemon_batches_many_terminal_digest_entries_into_one_edit(tmp_path, monkeypatch):
    conn=seeded(tmp_path/"m.db"); client=Discord(); snap=_seed_two_terminal_threads(conn,client)
    cfg=MirrorConfig(board="board",forum_channel_id="forum",guild_id="guild",terminal_lifecycle_enabled=True,done_thread_archive_idle_minutes=1)
    monkeypatch.setattr("plugins.platforms.discord.kanban_mirror.daemon.time.time",lambda:100)
    asyncio.run(_resume_terminal_lifecycles(cfg,client,conn,snap,load_mirror_state(conn),[]))
    assert client.events.count("digest")==1
    assert conn.execute("SELECT COUNT(*) FROM mirror_terminal_lifecycles WHERE state='tag_confirmed'").fetchone()[0]==2


def test_daemon_advances_only_entries_retained_by_bounded_digest(tmp_path, monkeypatch):
    conn=seeded(tmp_path/"m.db"); client=Discord(); snap=_seed_two_terminal_threads(conn,client)
    cfg=MirrorConfig(board="board",forum_channel_id="forum",guild_id="guild",terminal_lifecycle_enabled=True,done_thread_archive_idle_minutes=1)
    monkeypatch.setattr("plugins.platforms.discord.kanban_mirror.daemon.time.time",lambda:100)
    monkeypatch.setattr(DiscordLifecyclePublisher,"upsert_digest_batch",lambda self,entries:("digest",{"thread2"}))
    asyncio.run(_resume_terminal_lifecycles(cfg,client,conn,snap,load_mirror_state(conn),[]))
    rows={row["thread_id"]:row["state"] for row in conn.execute("SELECT thread_id,state FROM mirror_terminal_lifecycles")}
    assert rows=={"thread":"summary_confirmed","thread2":"tag_confirmed"}


def test_feature_gate_disabled_by_default_preserves_legacy():
    assert not load_mirror_config({}).terminal_lifecycle_enabled
    assert not MirrorConfig().terminal_lifecycle_enabled
