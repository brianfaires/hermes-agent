"""Single-board orchestration contract for #173, revised by ADR 0001.

The request closes after planning; Rook follows through on dependency-gated
milestone and close-out cards. Model decisions and worker processes are scripted.
"""
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_notify as kn


def test_request_plan_work_milestone_closeout_notifies_origin(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("kanban:\n  auto_decompose: false\n")
    for profile in ("rook", "ang", "ops"):
        profile_home = home / "profiles" / profile
        profile_home.mkdir(parents=True)
        (profile_home / "config.yaml").write_text("{}\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    board = "operations"
    kb.create_board(slug=board, name="Work")
    spawned = []

    def spawn(task, workspace, *, board):
        assert board == "operations"
        assert Path(workspace).is_relative_to(home)
        spawned.append((task.id, task.assignee))

    with kbc.connect_closing(board=board) as conn:
        request = kb.create_task(conn, title="Deliver request", assignee="rook", board=board)
        kn.add_notify_sub(conn, task_id=request, platform="telegram", chat_id="sunny-chat",
                          notifier_profile="default", delivery_mode="wake")

        def tick(expected):
            start = len(spawned)
            result = kbd.dispatch_once(conn, spawn_fn=spawn, board=board,
                                       max_in_progress_per_profile=1)
            assert spawned[start:] == expected
            assert [(row[0], row[1]) for row in result.spawned] == expected

        def finish(task_id, summary, **kwargs):
            task = kb.get_task(conn, task_id)
            assert task.status == "running"
            assert kb.complete_task(conn, task_id, summary=summary,
                                    expected_run_id=task.current_run_id, **kwargs)

        tick([(request, "rook")])
        engineering = kb.create_task(conn, title="Implement", assignee="ang", board=board,
                                     created_by="rook", creator_task_id=request)
        operations = kb.create_task(conn, title="Operate", assignee="ops", board=board,
                                    created_by="rook", creator_task_id=request, parents=[engineering])
        milestone = kb.create_task(conn, title="Judge result", assignee="rook", board=board,
                                   created_by="rook", creator_task_id=request, parents=[operations])
        closeout = kb.create_task(conn, title="Report result", assignee="rook", board=board,
                                  created_by="rook", creator_task_id=request, parents=[milestone])
        children = [engineering, operations, milestone, closeout]
        finish(request, "Plan recorded", created_cards=children)
        assert kb.get_task(conn, request).status == "done"
        assert kb.parent_ids(conn, request) == []
        assert [kb.get_task(conn, tid).status for tid in children] == [
            "ready", "todo", "todo", "todo",
        ]
        assert not kb.complete_task(conn, closeout, summary="Too early")

        tick([(engineering, "ang")])
        finish(engineering, "Implemented")
        tick([(operations, "ops")])
        finish(operations, "Operation verified")
        tick([(milestone, "rook")])
        finish(milestone, "Acceptance checked")
        tick([(closeout, "rook")])
        finish(closeout, "Result ready for Sunny")
        tick([])

        for tid in [request, *children]:
            assert kb.get_task(conn, tid).status == "done"
            sub, = kn.list_notify_subs(conn, tid)
            assert (sub["notifier_profile"], sub["delivery_mode"]) == ("default", "wake")
            _, events = kn.unseen_events_for_sub(conn, task_id=tid, platform="telegram",
                                               chat_id="sunny-chat", kinds=["completed"])
            assert len(events) == 1
