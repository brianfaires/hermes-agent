"""Selected worker launch prerequisites and executor invocation."""
from __future__ import annotations

import json
import uuid
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task


class WorkerCapabilityError(RuntimeError):
    """A selected launch prerequisite requires an owner action before retry."""


def validate_worker_skills(task: Task, profile_home: str) -> None:
    from agent.skill_commands import build_preloaded_skills_prompt
    from hermes_cli.kanban_db_dispatch import _worker_profile_scope

    if not task.skills:
        return
    with _worker_profile_scope(profile_home):
        _prompt, _loaded, missing = build_preloaded_skills_prompt(task.skills, task_id=task.id)
    if missing:
        raise WorkerCapabilityError(
            f"Profile {task.assignee}: required skill(s) unavailable: {', '.join(missing)}. "
            "The assigning owner must select a capable profile or repair the required skill, then unblock.")


def validate_worker_launch(task: Task, workspace: str) -> None:
    """Validate required skills and an enabled command route for a persistent workspace."""
    from hermes_cli.kanban_db_dispatch import _resolve_worker_cli_toolsets, _worker_profile_scope
    from hermes_cli.profiles import resolve_profile_env

    profile_home = resolve_profile_env(task.assignee or "default")
    validate_worker_skills(task, profile_home)
    toolsets = _resolve_worker_cli_toolsets(profile_home)
    if task.workspace_kind not in {"dir", "worktree"} or (toolsets and "terminal" not in toolsets):
        return
    from tools.terminal_tool import check_terminal_requirements, terminal_tool
    from tools.terminal_tool_backends import terminal_backend_unavailable_reason
    from tools.terminal_tool_lifecycle import cleanup_vm

    probe_id = f"kanban-preflight-{uuid.uuid4().hex}"
    with _worker_profile_scope(profile_home):
        if not check_terminal_requirements():
            raise WorkerCapabilityError(
                f"Profile {task.assignee} execution route unavailable: {terminal_backend_unavailable_reason()}. "
                "The assigning owner must repair the prerequisite or select a capable profile, then unblock.")
        try:
            result = json.loads(terminal_tool("echo hermes-launch-probe", task_id=probe_id,
                                               timeout=15, workdir=workspace))
        finally:
            cleanup_vm(probe_id)
    if result.get("error") or result.get("exit_code") != 0:
        reason = result.get("error") or result.get("output") or "command did not exit successfully"
        if (result.get("status") not in {"blocked", "disabled", "pending_approval"}
                and result.get("exit_code") not in {126, 127}):
            raise RuntimeError(f"Profile {task.assignee} workspace probe: {reason}")
        raise WorkerCapabilityError(
            f"Profile {task.assignee} cannot execute in {workspace}: {reason}. "
            "The assigning owner must repair the configured execution route or select a capable owner, then unblock.")


def _call_spawn_fn(spawn_fn, task: Task, workspace: str, board: Optional[str]) -> Optional[int]:
    """Back-compat: older spawn_fn signatures (and test stubs) accept only
    ``(task, workspace)``; pass ``board`` only when the callable supports it."""
    import inspect
    try:
        sig = inspect.signature(spawn_fn)
    except (TypeError, ValueError):
        return spawn_fn(task, workspace)
    if "board" in sig.parameters:
        return spawn_fn(task, workspace, board=board)
    return spawn_fn(task, workspace)


def spawn_worker(spawn_fn, task: Task, workspace: str, board: str | None) -> int | None:
    from hermes_cli.kanban_db_dispatch import _default_spawn

    if spawn_fn is None:
        validate_worker_launch(task, workspace)
        spawn_fn = _default_spawn
    return _call_spawn_fn(spawn_fn, task, workspace, board)
