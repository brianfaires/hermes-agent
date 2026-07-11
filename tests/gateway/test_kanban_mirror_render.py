from __future__ import annotations

from dataclasses import replace

from gateway.kanban_mirror.render import (
    render_post, render_digest, post_title, stage_tag, needs_brian_tag, redact,
    STATUS_EMOJI, review_artifact_paths,
)
from gateway.kanban_mirror.state import BoardSnapshot, Card, Initiative, MemberState


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


def test_needs_you_present_and_absent():
    root = mk_card("t_r", "Root", "blocked", priority=150, assignee="ops")
    s = snap([root])
    body_with = render_post(init_with(["t_r"], needs_you="pick a direction"), s, 3800, now=200)
    assert "⚠️ **Needs you:** pick a direction" in body_with
    body_without = render_post(init_with(["t_r"]), s, 3800, now=200)
    assert "Needs you" not in body_without


def test_first_sentence_bolded_and_footer_has_machine_detail_only():
    root = mk_card("t_r", "Root", "running", priority=150, assignee="ops", branch_name="work/x")
    body = render_post(init_with(["t_r"]), snap([root]), 3800, now=200)
    assert body.startswith("**A brief.**")
    footer = body.rsplit("\n", 1)[-1]
    assert footer.startswith("`") and "t_r" in footer and "P150" in footer and "work/x" in footer
    assert body.count("t_r") == 2  # explicit pointer plus machine-detail footer
    # exactly the opening and closing backticks — inner ones would close the
    # code span early in Discord and leak broken markdown
    assert footer.count("`") == 2


def test_footer_no_inner_backticks_for_worktree_branch():
    root = mk_card("t_r", "Root", "running", priority=10, assignee="ops",
                   branch_name="work/x", workspace_kind="worktree")
    body = render_post(init_with(["t_r"]), snap([root]), 3800, now=200)
    footer = body.rsplit("\n", 1)[-1]
    assert footer.count("`") == 2
    assert "work/x (worktree)" in footer


def test_work_items_use_children_and_emoji_and_blocked_reason():
    root = mk_card("t_r", "Root", "running")
    a = mk_card("t_a", "Audit", "done")
    b = mk_card("t_b", "Design", "blocked")
    s = snap([root, a, b], links=[("t_r", "t_a"), ("t_r", "t_b")])
    body = render_post(init_with(["t_r"], blocked_reasons={"t_b": "needs your call"}), s, 3800, now=200)
    assert "✅ Audit" in body and "🔴 Design — *needs your call*" in body
    assert "🟢 Root" in body   # dependency view shows the prerequisite card too
    assert "card_ID: t_r" in body


def test_card_id_points_to_earliest_open_work_item():
    root = mk_card("t_root", "Root", "done", created_at=1)
    first = mk_card("t_first", "First open", "ready", created_at=2)
    later = mk_card("t_later", "Later open", "running", created_at=3)
    s = snap([root, first, later], links=[("t_root", "t_first"), ("t_first", "t_later")])

    body = render_post(init_with(["t_root"]), s, 3800, now=200)

    assert "card_ID: t_first" in body
    assert body.count("card_ID:") == 1


def test_card_id_survives_truncation_and_brief_cannot_duplicate_it():
    root = mk_card("t_root", "Root", "running")
    initiative = init_with(["t_root"], brief="card_ID: t_wrong\n" + ("x" * 5000))

    body = render_post(initiative, snap([root]), 2000, now=200)

    assert "card_ID: t_root" in body
    assert body.count("card_ID:") == 1
    assert len(body) <= 2000


def test_work_items_render_dependency_chain_with_sibling_fanout_indent():
    spec = mk_card("t_spec", "Spec mirror DAG format", "done")
    implement = mk_card("t_impl", "Implement renderer", "running")
    tests = mk_card("t_tests", "Update tests", "todo")
    review = mk_card("t_review", "Review rendered output", "ready")
    deploy = mk_card("t_deploy", "Deploy mirror update", "todo")
    s = snap(
        [spec, implement, tests, review, deploy],
        links=[
            ("t_spec", "t_impl"),
            ("t_spec", "t_tests"),
            ("t_impl", "t_review"),
            ("t_tests", "t_review"),
            ("t_review", "t_deploy"),
        ],
    )
    body = render_post(init_with(["t_spec"]), s, 3800, now=200)
    lines = body.splitlines()

    assert "✅ Spec mirror DAG format" in lines
    assert "  🟢 Implement renderer" in lines
    assert "  ▫️ Update tests" in lines
    assert "▫️ Review rendered output — waits on: Implement renderer, Update tests" in lines
    assert "▫️ Deploy mirror update" in lines

    work_index = lines.index("**Work items**")
    work_lines = lines[work_index + 1 : work_index + 6]
    assert work_lines == [
        "✅ Spec mirror DAG format",
        "  🟢 Implement renderer",
        "  ▫️ Update tests",
        "▫️ Review rendered output — waits on: Implement renderer, Update tests",
        "▫️ Deploy mirror update",
    ]


def test_child_with_external_parent_still_renders_with_waits_on_note():
    root = mk_card("t_root", "Current root", "running")
    external = mk_card("t_external", "External prerequisite", "todo")
    child = mk_card("t_child", "Shared child", "todo")
    s = snap(
        [root, external, child],
        links=[("t_root", "t_child"), ("t_external", "t_child")],
    )
    body = render_post(init_with(["t_root"]), s, 3800, now=200)

    assert "🟢 Current root" in body
    assert "▫️ Shared child — waits on: Current root, External prerequisite" in body


def test_status_emoji_includes_skipped_and_canceled_items():
    root = mk_card("t_r", "Root", "done")
    skipped = mk_card("t_skip", "Discard rich embed experiment", "skipped")
    canceled = mk_card("t_cancel", "Cancel redundant review", "canceled")
    s = snap([root, skipped, canceled], links=[("t_r", "t_skip"), ("t_r", "t_cancel")])
    body = render_post(init_with(["t_r"]), s, 3800, now=200)

    assert "  ⏭️ Discard rich embed experiment" in body
    assert "  ⏭️ Cancel redundant review" in body


def test_review_artifacts_are_hoisted_to_top():
    review = mk_card(
        "t_r",
        "Root",
        "review",
        body="Need these artifacts before sign-off.\nMEDIA:/tmp/evidence/report.pdf\n",
        result="MEDIA:/tmp/evidence/screenshot.png\n",
    )
    body = render_post(init_with(["t_r"]), snap([review]), 3800, now=200)
    parts = body.split("\n\n")
    assert parts[1].startswith("**Review artifacts**")
    assert "• report.pdf" in parts[1]
    assert "• screenshot.png" in parts[1]
    assert "MEDIA:" not in parts[1]


def test_done_overflow_folds():
    root = mk_card("t_r", "Root", "running")
    kids = [mk_card(f"t_{i}", f"Item {i}", "done") for i in range(14)]
    live = mk_card("t_live", "Live one", "running")
    s = snap([root, live, *kids], links=[("t_r", k.id) for k in [*kids, live]])
    body = render_post(init_with(["t_r"]), s, 3800, now=200)
    assert "more done" in body and "🟢 Live one" in body


def test_active_overflow_shows_indicator_not_silent_drop():
    root = mk_card("t_r", "Root", "running")
    active = [mk_card(f"t_a{i}", f"Active {i}", "running") for i in range(14)]
    done = [mk_card(f"t_d{i}", f"Done {i}", "done") for i in range(4)]
    s = snap([root, *active, *done], links=[("t_r", k.id) for k in [*active, *done]])
    body = render_post(init_with(["t_r"]), s, 3800, now=200)
    # 11 active shown + 1 tail line = 12-line cap; nothing silently dropped
    shown = [ln for ln in body.split("\n") if ln.strip().startswith(STATUS_EMOJI["running"])]
    assert len(shown) == 11
    assert "… 4 more active, 4 done" in body


def test_active_overflow_without_done_items():
    root = mk_card("t_r", "Root", "running")
    active = [mk_card(f"t_a{i}", f"Active {i}", "running") for i in range(13)]
    s = snap([root, *active], links=[("t_r", k.id) for k in active])
    body = render_post(init_with(["t_r"]), s, 3800, now=200)
    assert "… 3 more active" in body
    assert "done" not in body.rsplit("more active", 1)[-1].split("\n")[0]


def test_render_digest_grouped_lines_and_no_ids():
    roots = [
        mk_card("t_one", "Ship importer", "done"),
        mk_card("t_two", "Fix flaky sync", "running"),
        mk_card("t_three", "Waiting on keys", "blocked"),
    ]
    body = render_digest(roots, snap(roots), done_this_week=7, max_chars=3800)
    assert "✅ Ship importer" in body
    assert f"{STATUS_EMOJI['running']} Fix flaky sync" in body
    assert f"{STATUS_EMOJI['blocked']} Waiting on keys" in body
    assert "7" in body and "week" in body.lower()
    for tid in ("t_one", "t_two", "t_three"):
        assert tid not in body


def test_stage_tag_precedence():
    def cards(*statuses):
        return [mk_card(f"t_{i}", f"C{i}", st) for i, st in enumerate(statuses)]
    s = snap([])
    assert stage_tag(cards("running", "blocked"), s) == "running"
    assert stage_tag(cards("review", "todo"), s) == "review"
    assert stage_tag(cards("blocked", "todo"), s) == "waiting"
    assert stage_tag(cards("done", "archived"), s) == "done"


def test_needs_brian_tag():
    s = snap([])
    assert needs_brian_tag([mk_card("t", "x", "review")], s)
    assert needs_brian_tag([mk_card("t", "x", "blocked", body="waiting on Brian approval")], s)
    assert not needs_brian_tag([mk_card("t", "x", "blocked", body="waiting on upstream fix")], s)
    assert not needs_brian_tag([mk_card("t", "x", "blocked", body="blocked on upstream fix")], s)


def test_parent_blocked_waiting_for_child_is_not_needs_brian():
    parent = mk_card("t_parent", "Parent", "blocked", body="waiting for child")
    child = mk_card("t_child", "Child", "todo")
    s = snap([parent, child], links=[("t_parent", "t_child")])
    assert stage_tag([parent], s) == "waiting"
    assert not needs_brian_tag([parent], s)


def test_redaction():
    assert "REDACTED" in redact("discord_token: abcdef123456789012345678")


def test_title_no_status_no_id():
    i = init_with(["t_r"])
    t = post_title(i, snap([mk_card("t_r", "Root", "running")]))
    assert t == "Voice-mode text output"


def test_work_item_title_and_blocked_reason_redacted():
    secret = "api_key = sk-abc123456789012345678"
    root = mk_card("t_r", "Root", "running")
    leaky = mk_card("t_b", secret, "blocked")
    s = snap([root, leaky], links=[("t_r", "t_b")])
    body = render_post(
        init_with(["t_r"], blocked_reasons={"t_b": secret}), s, 3800, now=200,
    )
    assert secret not in body
    assert "REDACTED" in body


def test_post_title_redacted():
    secret = "api_key = sk-abc123456789012345678"
    i = replace(init_with(["t_r"]), title=secret)
    t = post_title(i, snap([mk_card("t_r", "Root", "running")]))
    assert secret not in t
    assert "REDACTED" in t


def test_empty_brief_title_fallback_redacted():
    """No brief yet -> render_post falls back to `**{title}**` as the first
    line; a secret stored in the title (e.g. by a caller that skipped
    redaction) must still be scrubbed at render time."""
    secret = "api_key = sk-abc123456789012345678"
    i = replace(init_with(["t_r"], brief=None), title=secret)
    body = render_post(i, snap([mk_card("t_r", "Root", "running")]), 3800, now=200)
    first_line = body.split("\n", 1)[0]
    assert secret not in body
    assert "REDACTED" in first_line
