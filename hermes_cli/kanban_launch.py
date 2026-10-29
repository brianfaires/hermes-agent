"""Selected worker launch prerequisites and executor invocation."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Optional, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task

_log = logging.getLogger(__name__)


class WorkerCapabilityError(RuntimeError):
    """A selected launch prerequisite requires an owner action before retry."""


def lane_skills(profile_home: str | None, lane: str) -> list[str]:
    """Skills every worker the assigned profile runs in *lane* must load: its ``kanban.lane_skills``.

    Read from the ASSIGNEE's config, not the dispatcher's, because which review procedure a profile
    follows is that profile's policy. ``None`` (an assignee that does not resolve; launch validation
    reports that) applies the defaults. A malformed entry adds nothing (warned) rather than failing
    every spawn in the lane."""
    from hermes_cli.config import load_config
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from hermes_cli.kanban_db_dispatch import _worker_profile_scope

    if profile_home is None:
        configured = DEFAULT_CONFIG["kanban"]["lane_skills"]
    else:
        with _worker_profile_scope(profile_home):
            configured = ((load_config() or {}).get("kanban") or {}).get("lane_skills") or {}
    entry = configured.get(lane) if isinstance(configured, dict) else None
    if entry is None:
        return []
    if isinstance(entry, str):
        entry = [entry]
    if not isinstance(entry, list) or not all(isinstance(s, str) for s in entry):
        _log.warning("kanban.lane_skills.%s in %s is not a list of skill names; ignoring it", lane, profile_home)
        return []
    return list(dict.fromkeys(s.strip() for s in entry if s.strip()))


def resolve_worker_skills(task: Task, profile_home: str, required_skills: list[str],
                          *, workspace: str | None) -> list[str]:
    """Resolve current pins while retaining hard requirements of the assigned role."""
    from agent.skill_commands import _disabled_skill_names
    from agent.safe_worker_policy import safe_worker_enabled
    from agent.skill_utils import normalize_skill_lookup_name
    from tools.skills_tool import skill_view
    from hermes_cli.kanban_db_dispatch import _worker_profile_scope

    from agent.runtime_cwd import set_session_cwd, reset_session_cwd

    available, missing_required = [], []
    token = set_session_cwd(workspace)
    try:
        with _worker_profile_scope(profile_home):
            disabled = _disabled_skill_names()
            for identifier in dict.fromkeys([*required_skills, *(task.skills or [])]):
                payload = {} if safe_worker_enabled() else json.loads(skill_view(
                    normalize_skill_lookup_name(identifier), inspect_only=True))
                canonical = str(payload.get('name') or identifier)
                names = {identifier, canonical, canonical.split(':', 1)[-1]}
                if payload.get('success') and not names.intersection(disabled):
                    available.append(identifier)
                elif identifier in required_skills:
                    missing_required.append(identifier)
                else:
                    _log.info("Task %s: optional pinned skill unavailable in profile %s: %s",
                              task.id, task.assignee, identifier)
    finally:
        reset_session_cwd(token)
    if missing_required:
        raise WorkerCapabilityError(
            f"Profile {task.assignee}: required skill(s) unavailable: {', '.join(missing_required)}. "
            "The assigning owner must select a capable profile or repair the required skill, then unblock.")
    return available


def validate_worker_launch(task: Task, workspace: str, required_skills: list[str]) -> None:
    """Validate required skills and the selected workspace's enabled command route."""
    from hermes_cli.kanban_db_dispatch import _resolve_worker_cli_toolsets
    from hermes_cli.profiles import resolve_profile_env

    profile_home = resolve_profile_env(task.assignee or "default")
    resolve_worker_skills(task, profile_home, required_skills, workspace=workspace)
    toolsets = _resolve_worker_cli_toolsets(profile_home)
    if toolsets and "terminal" not in toolsets:
        return
    _probe_workspace(task, profile_home, workspace)


def _probe_workspace(task: Task, profile_home: str, workspace: str) -> None:
    from hermes_cli.kanban_db_dispatch import _worker_profile_scope
    from tools.terminal_scope import (
        enforce_no_refusal, get_terminal_scope, reset_terminal_scope, set_terminal_scope, terminal_env,
    )
    from tools.terminal_tool import (
        _resolve_container_task_id, check_terminal_requirements, clear_task_env_overrides,
        register_task_env_overrides, terminal_tool,
    )
    from tools.terminal_tool_backends import terminal_backend_unavailable_reason
    from tools.terminal_tool_lifecycle import cleanup_vm

    probe_id = f"kanban-preflight-{uuid.uuid4().hex}"
    with _worker_profile_scope(profile_home):
        enforce_no_refusal()
        if not check_terminal_requirements():
            raise WorkerCapabilityError(
                f"Profile {task.assignee} execution route unavailable: {terminal_backend_unavailable_reason()}. "
                "The assigning owner must repair the prerequisite or select a capable profile, then unblock.")
        scope = cast(dict[str, str], get_terminal_scope())  # Bound by _worker_profile_scope.
        token = set_terminal_scope({**scope, "TERMINAL_CWD": workspace})
        # A unique sandbox avoids changing or tearing down a live owner's environment.
        effective_id = probe_id
        try:
            register_task_env_overrides(probe_id, {"env_type": terminal_env("TERMINAL_ENV", "local"),
                                                  "cwd": workspace, "cwd_source": "session"})
            effective_id = _resolve_container_task_id(probe_id)
            result = json.loads(terminal_tool("echo hermes-launch-probe", task_id=probe_id, timeout=15))
        finally:
            try:
                cleanup_vm(effective_id, force_remove=True)
            finally:
                clear_task_env_overrides(probe_id)
                reset_terminal_scope(token)
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


def spawn_worker(spawn_fn, task: Task, workspace: str, board: str | None,
                 *, required_skills: list[str]) -> int | None:
    from hermes_cli.kanban_db_dispatch import _default_spawn

    if spawn_fn is None:
        validate_worker_launch(task, workspace, required_skills)
        spawn_fn = _default_spawn
    return _call_spawn_fn(spawn_fn, task, workspace, board)
