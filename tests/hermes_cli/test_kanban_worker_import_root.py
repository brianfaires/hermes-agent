"""A Kanban worker imports the install's own ``hermes_cli``, whatever its task workspace holds.

The dispatcher spawns ``python -m hermes_cli.kanban_worker_client`` with ``cwd=<workspace>``
and the install tree pinned on PYTHONPATH. Plain ``-m`` puts the cwd on ``sys.path`` ahead of
PYTHONPATH, so a workspace that is a hermes-agent worktree shadowed the install: the worker ran
the task branch's source, found no committed dependency environment for that tree, and crashed
before owner admission (brianfaires/hermes-agent#257).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_db import Task

INSTALL_ROOT = Path(dispatch.__file__).resolve().parent.parent


def _task() -> Task:
    return Task(
        id="t_import1", title="t", body=None, assignee="worker", status="running",
        priority=0, created_by=None, created_at=1, started_at=None, completed_at=None,
        workspace_kind="worktree", workspace_path=None, claim_lock=None,
        claim_expires=None, tenant=None,
    )


def test_worker_argv_keeps_the_workspace_off_the_import_path():
    argv = dispatch._worker_argv(_task(), "worker", None)
    assert argv == [sys.executable, "-P", "-m", "hermes_cli.kanban_worker_client"]


def test_worker_in_a_hermes_agent_workspace_imports_the_install_tree(tmp_path):
    workspace = tmp_path / "hermes-agent-worktree"
    (workspace / "hermes_cli").mkdir(parents=True)
    (workspace / "hermes_cli" / "__init__.py").write_text("")

    argv = dispatch._worker_argv(_task(), "worker", None)
    interpreter_flags = argv[1:argv.index("-m")]
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONSAFEPATH")}
    env = pin_hermes_tree_on_pythonpath(env, INSTALL_ROOT)
    probe = "import hermes_cli; print(hermes_cli.__file__)"

    out = subprocess.run(
        [argv[0], *interpreter_flags, "-c", probe],
        cwd=workspace, env=env, capture_output=True, text=True, check=True, timeout=60,
    ).stdout.strip()

    assert Path(out).resolve() == INSTALL_ROOT / "hermes_cli" / "__init__.py"
