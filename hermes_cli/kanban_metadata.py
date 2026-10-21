"""Shared workspace metadata checks for native creation and graph fan-out."""
from __future__ import annotations

from pathlib import Path


def normalize_branch_name(value, *, workspace_kind: str) -> str | None:
    """Validate a literal Git branch ref without resolving prior-checkout syntax."""
    branch = str(value).strip() if value is not None else ''
    if not branch:
        return None
    if workspace_kind != 'worktree':
        raise ValueError('branch_name is only valid for worktree workspaces')
    bad_character = any(ord(char) <= 32 or ord(char) == 127 or char in '~^:?*[\\' for char in branch)
    bad_component = any(part.startswith('.') or part.endswith('.lock') for part in branch.split('/'))
    bad_structure = (branch.startswith(('-', '/')) or branch.endswith(('/', '.'))
                     or '..' in branch or '@{' in branch or '//' in branch or branch == 'HEAD')
    if bad_character or bad_component or bad_structure:
        raise ValueError(f'invalid Git branch_name: {branch!r}')
    return branch


def normalize_workspace_metadata(kind: str, path, branch=None, *, require_dir_path: bool = False):
    """Keep native scratch/worktree defaults; reject metadata that cannot materialize."""
    from hermes_cli.kanban_db import VALID_WORKSPACE_KINDS

    if kind not in VALID_WORKSPACE_KINDS:
        raise ValueError(f'workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, got {kind!r}')
    normalized_path = None
    if path is not None:
        workspace = Path(path).expanduser()
        if not workspace.is_absolute():
            raise ValueError('workspace_path must be absolute')
        normalized_path = str(workspace)
    if require_dir_path and kind == 'dir' and normalized_path is None:
        raise ValueError("workspace_kind='dir' requires workspace_path or a board default_workdir")
    return kind, normalized_path, normalize_branch_name(branch, workspace_kind=kind)
