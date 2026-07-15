from __future__ import annotations

from dataclasses import replace

from gateway.kanban_mirror.planner import plan, Op, current_publish_hash
from gateway.kanban_mirror.config import MirrorConfig
from gateway.kanban_mirror.state import BoardSnapshot, Card, Initiative, MemberState, material_sig

CFG = MirrorConfig(enabled=True, board="b", forum_channel_id="f")


def mk_card(id, title, status, priority=0, body="", assignee=None,
            branch_name=None, workspace_kind="scratch", **kw):
    return Card(id=id, title=title, body=body, status=status, priority=priority,
                assignee=assignee, branch_name=branch_name, workspace_kind=workspace_kind,
                created_by=kw.get("created_by", "agent"), created_at=kw.get("created_at", 1),
                completed_at=kw.get("completed_at"),
                last_failure_error=kw.get("last_failure_error"), result=kw.get("result"))


def snap(cards, links=()):
    children, parents = {}, {}
    for p, c in links:
        children.setdefault(p, []).append(c)
        parents.setdefault(c, []).append(p)
    return BoardSnapshot({c.id: c for c in cards}, children, parents, {}, {})


def init_with(members, brief="A brief. More detail.", needs_you=None, blocked_reasons=None):
    return Initiative(id="init_x", title="Voice-mode text output", kind="post",
                      thread_id="th1", starter_message_id="m1", brief=brief,
                      needs_you=needs_you, brief_stale=False, brief_updated_at=100,
                      archived_at=None, blocked_reasons=blocked_reasons or {},
                      published_hash=None, created_at=0, updated_at=0,
                      members={m: MemberState(m, None, None) for m in members})


def kinds(ops):
    return [o.kind for o in ops]


def test_unassigned_active_root_triggers_curate():
    s = snap([mk_card("t_new", "New thing", "todo")])
    ops = plan(s, {}, None, set(), CFG, now=100)
    assert "curate" in kinds(ops)
    curate = next(o for o in ops if o.kind == "curate")
    assert curate.data["task_ids"] == ["t_new"]


def test_terminal_root_does_not_trigger_curate():
    s = snap([mk_card("t_old", "Done thing", "done")])
    assert "curate" not in kinds(plan(s, {}, None, set(), CFG, now=100))


def test_new_initiative_gets_create_thread():
    s = snap([mk_card("t_r", "Root", "running")])
    i = init_with(["t_r"], brief=None)
    i = replace(i, thread_id=None, starter_message_id=None)
    ops = plan(s, {i.id: i}, None, set(), CFG, now=100)
    assert "create_thread" in kinds(ops)


def test_all_members_done_updates_done_tag_then_archives_without_closure_note():
    s = snap([mk_card("t_r", "Root", "done")])
    i = init_with(["t_r"])   # member last_status "running"
    i.members["t_r"] = MemberState("t_r", "running", "oldsig")
    ops = plan(s, {i.id: i}, None, set(), CFG, now=100)
    ks = kinds(ops)
    assert "edit_post" in ks and "archive_thread" in ks
    assert "post_note" not in ks
    assert ks.index("edit_post") < ks.index("archive_thread")
    edit = next(o for o in ops if o.kind == "edit_post")
    assert "done" in edit.data["tags"]


def test_note_dedup_by_key():
    s = snap([mk_card("t_r", "Root", "done")])
    i = init_with(["t_r"])
    i.members["t_r"] = MemberState("t_r", "running", "oldsig")
    ops = plan(s, {i.id: i}, None, {"done:t_r", f"alldone:{i.id}"}, CFG, now=100)
    assert "post_note" not in kinds(ops)


def test_material_change_marks_stale_and_updates_member():
    s = snap([mk_card("t_r", "Root", "blocked")])
    i = init_with(["t_r"])
    i.members["t_r"] = MemberState("t_r", "blocked", "different-sig")
    ops = plan(s, {i.id: i}, None, set(), CFG, now=100)
    assert "mark_stale" in kinds(ops) and "member_seen" in kinds(ops)


def test_member_done_note_retried_for_partial_completion_after_last_status_already_advanced():
    """Simulates: previous tick's post_note send failed, but member_seen had
    already committed the terminal status (member_seen/note are decoupled,
    so a failed send must not be lost once note_keys lacks the key)."""
    s = snap([mk_card("t_r", "Root", "done"), mk_card("t_active", "Still active", "running")])
    i = init_with(["t_r", "t_active"])
    i.members["t_r"] = MemberState("t_r", "done", "somesig")  # last_status already "done"
    i.members["t_active"] = MemberState("t_active", "running", "activesig")
    ops = plan(s, {i.id: i}, None, set(), CFG, now=100)  # note_keys empty: send never landed
    ks = kinds(ops)
    assert "post_note" in ks
    note_op = next(o for o in ops if o.kind == "post_note")
    assert note_op.data["note_key"] == "done:t_r"


def test_member_done_note_not_reemitted_once_recorded():
    s = snap([mk_card("t_r", "Root", "done")])
    i = init_with(["t_r"])
    i.members["t_r"] = MemberState("t_r", "done", "somesig")
    ops = plan(s, {i.id: i}, None, {"done:t_r", f"alldone:{i.id}"}, CFG, now=100)
    assert "post_note" not in kinds(ops)


def test_blocked_note_retried_after_last_status_already_advanced():
    """Same loss mode as member_done: if member_seen commits the blocked
    status before the note lands, the flip is no longer detectable next
    tick — the belt-and-braces retry must still catch it via note_keys."""
    root = mk_card("t_r", "Root", "blocked", body="needs brian to authorize")
    s = snap([root])
    i = init_with(["t_r"])
    i.members["t_r"] = MemberState("t_r", "blocked", "somesig")  # already advanced
    ops = plan(s, {i.id: i}, None, set(), CFG, now=100)  # no blocked:* note_key recorded
    note_ops = [o for o in ops if o.kind == "post_note" and o.data["note_kind"] == "initiative_blocked"]
    assert len(note_ops) == 1
    assert note_ops[0].data["note_key"] == "blocked:t_r:blocked"


def test_blocked_note_not_reemitted_once_recorded():
    root = mk_card("t_r", "Root", "blocked", body="needs brian to authorize")
    s = snap([root])
    i = init_with(["t_r"])
    i.members["t_r"] = MemberState("t_r", "blocked", "somesig")
    ops = plan(s, {i.id: i}, None, {"blocked:t_r:blocked"}, CFG, now=100)
    assert not [o for o in ops if o.kind == "post_note" and o.data["note_kind"] == "initiative_blocked"]


def test_unchanged_initiative_produces_no_discord_ops():
    card = mk_card("t_r", "Root", "running")
    s = snap([card])
    i = init_with(["t_r"])
    i.members["t_r"] = MemberState("t_r", "running", material_sig(card, []))
    i = replace(i, published_hash=current_publish_hash(i, s, CFG))  # helper exported by planner
    ops = plan(s, {i.id: i}, None, set(), CFG, now=100)
    assert all(o.kind == "curate" or o.kind not in
               {"create_thread", "edit_post", "archive_thread", "post_note"} for o in ops)
