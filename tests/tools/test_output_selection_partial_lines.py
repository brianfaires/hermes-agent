"""Output handed on mid-stream never carries a secret cut by its window or by an unfinished line."""
import json
import shlex
import sys
import threading
import time

import pytest

from agent import redact
from tools import interrupt as interrupt_mod
from tools import process_registry as pr
from tools.registry import registry as tool_registry
from tests.waiting import wait_until


@pytest.mark.platforms("linux")
def test_yield_to_background_redacts_returned_window_and_adopted_seed(tmp_path, monkeypatch):
    from tools.terminal_tool import terminal_tool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(redact, "_REDACT_ENABLED", True)
    monkeypatch.setattr("tools.tool_output_limits.get_max_bytes", lambda: 300)
    printed, gate = tmp_path / "printed", tmp_path / "gate"
    script = (
        "import pathlib,time;print('HEAD');"
        "print(\"RuntimeError: {'API_KEY': '\" + 'opaque-yield-fragment-' * 300 + \"'}\");"
        f"print('TAIL',flush=True);pathlib.Path({str(printed)!r}).touch();g=pathlib.Path({str(gate)!r})\n"
        "while not g.exists(): time.sleep(.02)")
    result = {}

    def worker():
        result.update(json.loads(terminal_tool(shlex.join([sys.executable, "-c", script]),
                                               task_id="yield-redaction", timeout=60)))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    wait_until(printed.exists, timeout=15, what="the command printed its output")
    interrupt_mod.request_yield(thread.ident)
    thread.join(timeout=15)
    assert result.get("status") == "yielded_to_background", result
    try:
        assert "opaque-yield-fragment" not in result["output"]
        session = pr.process_registry.get(result["session_id"])
        assert "opaque-yield-fragment" not in session.output_buffer
        assert "opaque-yield-fragment" not in pr.process_registry.poll(session.id)["output_preview"]
    finally:
        gate.touch()
        wait_until(lambda: pr.process_registry.get(result["session_id"]).exited, timeout=15,
                   what="the adopted process exited")


def test_running_unterminated_line_is_held_until_it_ends(monkeypatch):
    from tui_gateway import server

    monkeypatch.setattr(redact, "_REDACT_ENABLED", True)
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    session = pr.ProcessSession(id="proc_partial", command="worker", started_at=time.time(),
                                session_key="owner", heartbeat_seconds=60, watch_patterns=["token"])
    registry._running[session.id] = session

    def views():
        poll = json.loads(tool_registry.dispatch("process_manage", {"action": "poll", "session_id": session.id}))
        log = json.loads(tool_registry.dispatch("process_manage", {"action": "log", "session_id": session.id}))
        row = server._session_processes({"session_key": "owner"})[0]
        return poll["output_preview"] + log["output"] + row["output_tail"] + row["output_preview"]

    session.append_output("ready\ntoken ghp_abcdefgh")
    seen = views()
    assert "ready" in seen and "cdefgh" not in seen
    registry._check_watch_patterns(session, "token ghp_abcdefgh")
    registry._emit_heartbeat(session, time.time())
    for _ in range(2):
        assert "cdefgh" not in registry.completion_queue.get_nowait()["output"]

    session.append_output("ijklmnopqrstuvwxyz0123456789ABCD\n")
    seen = views()
    assert "token ghp_ab" in seen and "cdefgh" not in seen
