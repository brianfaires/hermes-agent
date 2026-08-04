"""Delegated-agent isolation from a Kanban worker's lifecycle authority."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from gateway.session_context import get_session_env
from hermes_cli import kanban_db as kb
from model_tools import get_tool_definitions
from tools.delegate_tool import delegate_task
from tools.environments.local import hermes_subprocess_env
from tools.kanban_tools import _handle_block


def _make_parent() -> MagicMock:
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "test-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = None
    parent._print_fn = None
    parent._current_task_id = "parent-agent-turn"
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.enabled_toolsets = ["terminal", "file", "kanban"]
    parent.disabled_toolsets = []
    parent.valid_tool_names = {
        "terminal",
        "read_file",
        "kanban_show",
        "kanban_block",
        "kanban_complete",
    }
    return parent


def test_delegate_cannot_block_parent_but_keeps_safe_terminal_coding(
    monkeypatch, tmp_path: Path
) -> None:
    """Exercise the real delegated-child thread and subprocess boundary.

    The child deliberately tries the exact incident path (kanban_block with the
    parent's task id), inspects its loaded schemas, and spawns a coding
    subprocess. The parent task must stay running while ordinary workspace file
    creation remains available.
    """
    parent_home = tmp_path / "parent-kanban"
    parent_db = parent_home / "kanban.db"
    parent_home.mkdir()
    with kb.connect(parent_db) as conn:
        task_id = kb.create_task(
            conn,
            title="parent worker",
            assignee="ang",
            initial_status="running",
        )
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
        conn.commit()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output_path = workspace / "child-output.txt"

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(parent_db))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(parent_home))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "parent-claim")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(parent_home / "workspaces"))
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(parent_home / "attachments"))

    observed: dict[str, object] = {}
    child = MagicMock()
    child._credential_pool = None
    child._subagent_id = None
    child._delegate_saved_tool_names = []
    child._delegate_role = "leaf"
    child.tool_progress_callback = None
    child.model = "test-child"
    child.session_prompt_tokens = 0
    child.session_completion_tokens = 0
    child.session_estimated_cost_usd = 0.0

    def _run_child(*, user_message, task_id, stream_callback):
        observed["task_context"] = get_session_env("HERMES_KANBAN_TASK", "")
        definitions = get_tool_definitions(
            enabled_toolsets=["terminal", "file", "kanban"],
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
        observed["tool_names"] = {
            item["function"]["name"] for item in definitions
        }
        observed["block_result"] = json.loads(
            _handle_block(
                {
                    "task_id": task_id_from_parent,
                    "reason": "delegated child must not own this lifecycle",
                    "kind": "needs_input",
                }
            )
        )
        observed["workspace_root"] = kb.workspaces_root()
        observed["attachments_root"] = kb.attachments_root()
        child_env = hermes_subprocess_env(inherit_credentials=True)
        observed["child_env"] = child_env
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('safe coding works')",
                str(output_path),
            ],
            cwd=workspace,
            env=child_env,
            check=False,
            capture_output=True,
            text=True,
        )
        observed["subprocess_returncode"] = proc.returncode
        return {
            "final_response": "isolation probe complete",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

    task_id_from_parent = task_id
    child.run_conversation.side_effect = _run_child

    with patch("run_agent.AIAgent", return_value=child):
        result = json.loads(
            delegate_task(
                goal="Probe parent lifecycle isolation and write a safe file",
                parent_agent=_make_parent(),
            )
        )

    assert result["results"][0]["status"] == "completed"
    with kb.connect(parent_db) as conn:
        assert kb.get_task(conn, task_id).status == "running"

    assert observed["subprocess_returncode"] == 0
    assert output_path.read_text() == "safe coding works"

    tool_names = observed["tool_names"]
    assert "terminal" in tool_names
    assert not any(name.startswith("kanban_") for name in tool_names)
    assert observed["task_context"] == ""
    assert observed["workspace_root"] != parent_home / "workspaces"
    assert observed["attachments_root"] != parent_home / "attachments"

    child_env = observed["child_env"]
    assert isinstance(child_env, dict)
    assert child_env.get("HERMES_DELEGATED_CHILD") == "1"
    assert not child_env.get("HERMES_KANBAN_TASK")
    assert child_env.get("HERMES_KANBAN_DB") != str(parent_db)
    assert child_env.get("HERMES_KANBAN_WORKSPACES_ROOT") != str(
        parent_home / "workspaces"
    )
    assert child_env.get("HERMES_KANBAN_ATTACHMENTS_ROOT") != str(
        parent_home / "attachments"
    )
    assert observed["workspace_root"] == Path(
        child_env["HERMES_KANBAN_WORKSPACES_ROOT"]
    )
    assert observed["attachments_root"] == Path(
        child_env["HERMES_KANBAN_ATTACHMENTS_ROOT"]
    )
    for key in (
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
    ):
        assert key not in child_env

    assert "error" in observed["block_result"]
