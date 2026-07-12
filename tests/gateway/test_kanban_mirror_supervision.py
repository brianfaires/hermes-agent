import asyncio

import pytest

from gateway.kanban_mirror.schema import initialize_mirror_schema
from gateway.kanban_mirror.state import connect_mirror
from gateway.kanban_mirror.supervision import LoopSupervisor, health_snapshot


@pytest.mark.asyncio
async def test_supervisor_deduplicates_restarts_and_awaits_shutdown():
    sleeps = []
    release = asyncio.Event()
    calls = 0

    async def sleep(delay):
        sleeps.append(delay)

    async def runner():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        await release.wait()

    supervisor = LoopSupervisor(sleep=sleep, jitter=lambda: 0.5, base_backoff=2)
    first = supervisor.start("inbound", runner)
    assert supervisor.start("inbound", runner) is first
    for _ in range(10):
        await asyncio.sleep(0)
        if calls == 2:
            break
    assert calls == 2
    assert sleeps == [2]
    assert supervisor.snapshot()["inbound"]["restarts"] == 1
    assert "boom" in supervisor.snapshot()["inbound"]["last_error"]
    await supervisor.stop()
    assert first.done()
    assert supervisor.snapshot()["inbound"]["state"] == "stopped"


def test_health_snapshot_is_bounded_content_free_and_disabled_is_silent(tmp_path):
    conn = connect_mirror(tmp_path / "mirror.db")
    initialize_mirror_schema(conn)
    supervisor = LoopSupervisor()
    assert health_snapshot(conn, router_enabled=False, ingress_connected=False,
                           adapters={}, supervisor=supervisor, now=100) == {}
    conn.execute("""INSERT INTO mirror_discord_inbound_state
      (discord_message_id,thread_id,conversation_event_id,classification,processing_status,observed_via,observed_at,payload)
      VALUES ('m','t','e','pending','pending','live',80,'secret inbound')""")
    conn.execute("""INSERT INTO mirror_discord_outbox
      (operation_id,correlation_id,target_profile,thread_id,payload,payload_hash,status,created_at,updated_at)
      VALUES ('o','c','ops','t','secret outbound','h','pending',70,70)""")
    conn.commit()
    adapter = type("Adapter", (), {
        "is_connected": False,
        "kanban_supervisor_snapshot": lambda self: {
            "pending-inbound": {"state": "running", "restarts": 1, "last_error": "boom"},
            "reconnect-backfill": {"state": "backoff", "restarts": 2, "last_error": "fetch"},
        },
    })()
    result = health_snapshot(conn, router_enabled=True, ingress_connected=True,
                             adapters={"ops": adapter}, supervisor=supervisor,
                             now=100, backlog_limit=0)
    assert result["pending_inbound"] == {"count": 1, "oldest_age_seconds": 20}
    assert result["outbox"]["pending"] == 1
    assert result["outbox"]["oldest_age_seconds"] == 30
    assert result["cursor"] == {"lag": 0, "backlog_limited": True}
    assert result["profile_adapters"] == {"ops": False}
    assert result["adapter_supervisors"]["ops"]["pending-inbound"]["restarts"] == 1
    assert result["adapter_supervisors"]["ops"]["reconnect-backfill"]["state"] == "backoff"
    assert "secret" not in repr(result)
