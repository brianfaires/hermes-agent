"""Resolve Discord mirror threads into Kanban context for gateway turns."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MirroredKanbanTask:
    task_id: str
    title: str | None = None
    status: str | None = None
    assignee: str | None = None
    priority: int | None = None


@dataclass(frozen=True)
class MirroredKanbanThreadContext:
    board_slug: str
    initiative_id: str
    initiative_kind: str
    thread_id: str
    starter_message_id: str | None
    task_ids: list[str]
    tasks: dict[str, MirroredKanbanTask] = field(default_factory=dict)
    primary_task_id: str | None = None
    safe_default_task_id: str | None = None

    @property
    def is_multi_card(self) -> bool:
        return self.safe_default_task_id is None


def _fetch_tasks(board_slug: str, task_ids: list[str]) -> dict[str, MirroredKanbanTask]:
    if not task_ids:
        return {}
    try:
        from hermes_cli import kanban_db as kb

        # Current Kanban architecture resolves explicit boards through the
        # canonical path helper (which also honors registered real DB paths).
        db_path = kb.kanban_db_path(board_slug)
    except Exception:
        return {}
    if not db_path.exists():
        return {}
    placeholders = ",".join("?" for _ in task_ids)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT id, title, status, assignee, priority FROM tasks WHERE id IN ({placeholders})",
            task_ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {
        row["id"]: MirroredKanbanTask(
            task_id=row["id"],
            title=row["title"],
            status=row["status"],
            assignee=row["assignee"],
            priority=row["priority"],
        )
        for row in rows
    }


def _resolve_in_board(board_slug: str, mirror_path: Path, thread_id: str) -> MirroredKanbanThreadContext | None:
    if not mirror_path.exists():
        return None
    conn = sqlite3.connect(f"file:{mirror_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        try:
            initiative = conn.execute(
                """
                SELECT id, kind, thread_id, starter_message_id
                FROM mirror_initiatives
                WHERE thread_id = ?
                """,
                (thread_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if initiative is None:
            return None
        members = conn.execute(
            """
            SELECT task_id FROM mirror_members
            WHERE initiative_id = ?
            ORDER BY rowid ASC
            """,
            (initiative["id"],),
        ).fetchall()
    finally:
        conn.close()

    task_ids = [str(row["task_id"]) for row in members if str(row["task_id"] or "").strip()]
    primary_task_id = task_ids[0] if task_ids else None
    safe_default_task_id = primary_task_id if initiative["kind"] != "digest" and len(task_ids) == 1 else None
    return MirroredKanbanThreadContext(
        board_slug=board_slug,
        initiative_id=str(initiative["id"]),
        initiative_kind=str(initiative["kind"]),
        thread_id=str(initiative["thread_id"]),
        starter_message_id=initiative["starter_message_id"],
        task_ids=task_ids,
        tasks=_fetch_tasks(board_slug, task_ids),
        primary_task_id=primary_task_id,
        safe_default_task_id=safe_default_task_id,
    )


# Resolution scans every board's mirror DB; a turn resolves the same thread
# more than once (prompt notes + session vars), so cache briefly by thread id.
_RESOLVE_CACHE: dict[str, tuple[float, "MirroredKanbanThreadContext | None"]] = {}
_RESOLVE_CACHE_TTL_SECONDS = 30.0
_RESOLVE_CACHE_MAX_ENTRIES = 512


def _clear_resolve_cache() -> None:
    _RESOLVE_CACHE.clear()


def resolve_mirrored_kanban_thread(thread_id: str | None) -> MirroredKanbanThreadContext | None:
    """Find Kanban mirror metadata for a Discord thread id across boards.

    A non-digest initiative with exactly one mirror member is a safe default
    task. Parent-card threads with child rollups still meet this rule because
    only the parent/root card is a mirror member; children are render context.
    Digest or grouped initiative threads remain multi-card and intentionally do
    not pick a task silently.
    """
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        return None
    now = time.monotonic()
    cached = _RESOLVE_CACHE.get(thread_id)
    if cached is not None and now - cached[0] < _RESOLVE_CACHE_TTL_SECONDS:
        return cached[1]
    resolved = _resolve_mirrored_kanban_thread_uncached(thread_id)
    if len(_RESOLVE_CACHE) >= _RESOLVE_CACHE_MAX_ENTRIES:
        _RESOLVE_CACHE.clear()
    _RESOLVE_CACHE[thread_id] = (now, resolved)
    return resolved


def _resolve_mirrored_kanban_thread_uncached(thread_id: str) -> MirroredKanbanThreadContext | None:
    try:
        from hermes_cli import kanban_db as kb
        from plugins.platforms.discord.kanban_mirror.state import mirror_db_path

        boards = [str(row.get("slug") or row.get("name") or "") for row in kb.list_boards(include_archived=False)]
    except Exception:
        return None

    matches: list[MirroredKanbanThreadContext] = []
    for board_slug in boards:
        if not board_slug:
            continue
        try:
            resolved = _resolve_in_board(board_slug, mirror_db_path(board_slug), thread_id)
        except Exception:
            resolved = None
        if resolved is not None:
            matches.append(resolved)

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    # A Discord thread id should be globally unique, but if stale mirror DBs
    # disagree, keep the context visible while refusing a silent task default.
    first = matches[0]
    task_ids: list[str] = []
    tasks: dict[str, MirroredKanbanTask] = {}
    for match in matches:
        task_ids.extend(match.task_ids)
        tasks.update(match.tasks)
    return MirroredKanbanThreadContext(
        board_slug=first.board_slug,
        initiative_id=first.initiative_id,
        initiative_kind="ambiguous",
        thread_id=thread_id,
        starter_message_id=first.starter_message_id,
        task_ids=task_ids,
        tasks=tasks,
        primary_task_id=None,
        safe_default_task_id=None,
    )


def render_mirrored_kanban_context(ctx: MirroredKanbanThreadContext) -> str:
    lines = [
        "**Linked Kanban mirror:**",
        f"  - Board: `{ctx.board_slug}`",
        f"  - Initiative: `{ctx.initiative_id}` (`{ctx.initiative_kind}`)",
        f"  - Discord thread: `{ctx.thread_id}`",
    ]
    if ctx.safe_default_task_id:
        lines.append(f"  - Primary task: `{ctx.safe_default_task_id}`")
        task = ctx.tasks.get(ctx.safe_default_task_id)
        if task:
            details = []
            if task.title:
                details.append(task.title)
            if task.status:
                details.append(f"status={task.status}")
            if task.assignee:
                details.append(f"assignee={task.assignee}")
            if details:
                lines.append(f"  - Primary task details: {'; '.join(details)}")
        lines.append("  - Default Kanban tools target this task unless the user names another task.")
    else:
        task_list = ", ".join(f"`{tid}`" for tid in ctx.task_ids) or "none"
        lines.append(f"  - Linked tasks: {task_list}")
        lines.append("  - Multi-card thread: do not choose a Kanban task silently; ask or use an explicit task id.")
    return "\n".join(lines)
