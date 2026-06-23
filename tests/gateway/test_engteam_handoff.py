from gateway.engteam_handoff import handoff_engineering


def test_handoff_opens_project_and_acks():
    calls = {}
    def fake_opener(*, goal, **kw):
        calls["goal"] = goal
        class P: root_id = "t_root"; pass
        return P()
    ack = handoff_engineering("please add a CSV export endpoint", opener=fake_opener)
    assert "CSV export" in calls["goal"]
    assert "engineering" in ack.lower()


def test_orchestrator_routes_engineering_to_handoff():
    from gateway.front_desk_orchestrator import plan_front_desk_turn
    cfg = {"agent": {"front_desk": {"enabled": True}}}
    plan = plan_front_desk_turn("there's a bug in the auth code", config=cfg)
    assert plan.action == "handoff"
    assert plan.decision.team == "engineering"
