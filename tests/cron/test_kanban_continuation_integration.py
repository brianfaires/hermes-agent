"""Native cron scripts + board + sessions; no external model/API execution."""
import json
import shlex
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from cron.jobs import create_job, get_job, pause_job, use_cron_store
from cron.scheduler import SILENT_MARKER, run_job
from hermes_cli import kanban_db as kb
from hermes_state import SessionDB


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "scripts").mkdir(parents=True)
    (home / "profiles" / "ang").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "board.db"))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    source_root = str(Path(__file__).resolve().parents[2])
    with use_cron_store(home), kb.connect_closing() as conn:
        job = create_job(prompt="Read the admitted card; apply autonomous-work-continuation. Recheck gates; acknowledge only verified action/closeout.",
                         schedule="every 5m", script="gate.sh", monitor_script="gate.sh",
                         workdir=source_root,
                         provider="openai-codex", model="gpt-6.1-sol", subscription_only=True,
                         attach_to_session=False, deliver="local")
        script = home / "scripts" / "gate.sh"
        script.write_text(
            f"exec {shlex.quote(sys.executable)} - <<'PY'\n"
            "from pathlib import Path\n"
            f"p = Path({str(home / 'scripts' / 'calls.txt')!r})\n"
            "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
            "from hermes_cli.kanban_continuation import emit_gate\n"
            f"emit_gate(job_id={job['id']!r}, profile='default')\nPY\n", encoding="utf-8")
        task = kb.create_task(conn, title="Scoped authorized work", assignee="ang",
                              subscription_only=True, provider_override="openai-codex",
                              model_override="gpt-6.1-sol")
        kb.block_task(conn, task, reason="temporary waiting", kind="transient")
        kb.add_notify_sub(conn, task_id=task, platform="continuation", chat_id=job["id"],
                          notifier_profile="default", delivery_metadata={
                              "authority_actor": "Brian", "authority_reference": "test approval",
                              "authority_expires_at": int(time.time()) + 600,
                              "authority_task_id": task, "authority_assignee": "ang",
                              "procedure": "autonomous-work-continuation"})
        yield home, conn, job, task


def install_model_sentinel(monkeypatch):
    """Only the forbidden network/model boundary is replaced; native IO runs."""
    from agent.inference_policy import subscription_only_active
    import cron.scheduler as sched
    from hermes_cli import runtime_provider

    observed = []

    class SentinelAgent:
        def __init__(self, **kwargs):
            assert subscription_only_active()
            assert kwargs["provider"] == "openai-codex"
            assert kwargs["model"] == "gpt-6.1-sol"
            self.kwargs = kwargs

        def run_conversation(self, prompt, **kwargs):
            assert subscription_only_active()
            observed.append((self.kwargs, prompt))
            return {"final_response": "verified local sentinel boundary", "messages": []}

        def get_activity_summary(self):
            return {"seconds_since_activity": 0.0}

    module = ModuleType("run_agent")
    module.AIAgent = SentinelAgent
    monkeypatch.setitem(sys.modules, "run_agent", module)
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda **kwargs: {
        "provider": "openai-codex", "api_mode": "codex_responses",
        "base_url": "https://chatgpt.com/backend-api/codex",
    })
    monkeypatch.setattr(sched, "_resolve_origin", lambda job: None)
    monkeypatch.setattr(sched, "_resolve_delivery_target", lambda job: None)
    monkeypatch.setattr(sched, "_resolve_cron_enabled_toolsets", lambda job, cfg: [])
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")
    return observed


def test_repeated_blocked_script_checks_have_zero_models_or_transcript_appends(setup, monkeypatch):
    home, conn, job, task = setup
    observed = install_model_sentinel(monkeypatch)
    with SessionDB() as db:
        db.create_session("brian-conversation", "telegram")
        before = db.get_messages("brian-conversation")
        for _ in range(4):
            ok, doc, final, error = run_job(get_job(job["id"]))
            assert ok and error is None and final == SILENT_MARKER
        assert db.get_messages("brian-conversation") == before
    assert observed == []
    assert int((home / "scripts" / "calls.txt").read_text()) == 4
    assert get_job(job["id"]).get("monitor_state") is None


def test_missed_event_poll_admits_one_fresh_bounded_session(setup, monkeypatch):
    home, conn, job, task = setup
    observed = install_model_sentinel(monkeypatch)
    # No notifier or event hook runs: polling must recover this transition.
    kb.unblock_task(conn, task)
    kb._record_task_failure(conn, task, "authentication failed", outcome="spawn_failed", failure_limit=5)
    ok, doc, final, error = run_job(get_job(job["id"]))
    assert ok and error is None
    assert len(observed) == 1
    kwargs, prompt = observed[0]
    assert kwargs["platform"] == "cron"
    assert kwargs["session_id"].startswith(f"cron_{job['id']}_")
    assert kwargs["session_id"] != "brian-conversation"
    assert len(prompt) < 600 and task in prompt
    assert "Monitor Baseline" not in prompt and "CHANGE DETECTED" not in prompt
    # The shared script executes ONCE on the admitted run, not twice.
    assert int((home / "scripts" / "calls.txt").read_text()) == 1
    # Model success without a verified card effect must remain retryable.
    assert run_job(get_job(job["id"]))[0]
    assert len(observed) == 2


@pytest.mark.parametrize("failure", [RuntimeError, InterruptedError])
def test_failed_or_interrupted_admission_is_retryable(setup, monkeypatch, failure):
    home, conn, job, task = setup
    observed = install_model_sentinel(monkeypatch)
    kb.unblock_task(conn, task)
    kb._record_task_failure(conn, task, "authentication failed", outcome="spawn_failed", failure_limit=5)
    agent = sys.modules["run_agent"].AIAgent
    original = agent.run_conversation
    def fail(self, *args, **kwargs):
        raise failure("sentinel interrupted before any verified effect")
    monkeypatch.setattr(agent, "run_conversation", fail)
    assert not run_job(get_job(job["id"]))[0]
    monkeypatch.setattr(agent, "run_conversation", original)
    assert run_job(get_job(job["id"]))[0]
    assert len(observed) == 1


def test_paused_job_does_not_admit_on_script_or_manual_scoped_call(setup, monkeypatch):
    home, conn, job, task = setup
    observed = install_model_sentinel(monkeypatch)
    kb.unblock_task(conn, task)
    pause_job(job["id"], reason="operator hold")
    assert run_job(get_job(job["id"]))[2] == SILENT_MARKER
    assert observed == []
