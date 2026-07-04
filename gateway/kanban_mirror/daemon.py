"""Daemon loop for the Kanban Discord mirror.

Wires together ``state`` (mirror.db + board snapshot), ``planner`` (pure op
plan), ``writer`` (LLM curation/prose/notes), and ``discord_client`` (HTTP)
into one tick: load -> plan -> execute -> (rate-limited) prose pass.

Every sqlite and Discord call is synchronous under the hood, so each one is
dispatched via ``asyncio.to_thread`` — the gateway event loop this runs
inside must never block on I/O.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from gateway.kanban_mirror.closed_thread_policy import classify_thread_state, resolve_closed_thread_action
from gateway.kanban_mirror.config import MirrorConfig
from gateway.kanban_mirror.discord_client import (
    DiscordAPIError,
    DiscordClient,
    ensure_forum_tags,
    load_discord_token,
    split_discord_message,
)
from gateway.kanban_mirror.planner import Op, _digest_hash, _tags_for, current_publish_hash, plan
from gateway.kanban_mirror.render import (
    post_title,
    redact,
    render_digest,
    render_post,
    review_artifact_paths,
)
from gateway.kanban_mirror.state import (
    BoardSnapshot,
    Initiative,
    MemberState,
    add_member,
    connect_mirror,
    create_initiative,
    get_digest,
    load_board_snapshot,
    load_mirror_state,
    load_note_keys,
    mark_brief_stale,
    mirror_db_path,
    record_note,
    set_archived,
    set_member_seen,
    set_prose,
    set_thread,
)
from gateway.kanban_mirror import writer
from gateway.kanban_mirror.writer import WriterError

logger = logging.getLogger(__name__)

# Prose-pass rate limiting + per-initiative backoff on write_prose failures.
_LAST_PROSE_PASS: float = 0.0
_PROSE_BACKOFF: dict[str, tuple[int, float]] = {}
_PROSE_BACKOFF_BASE = 60.0
_PROSE_BACKOFF_CAP = 3600.0


# ---------------------------------------------------------------------------
# small internal helpers
# ---------------------------------------------------------------------------


def _store_published_hash(conn: sqlite3.Connection, initiative_id: str, h: str) -> None:
    conn.execute(
        "UPDATE mirror_initiatives SET published_hash = ?, updated_at = ? WHERE id = ?",
        (h, int(time.time()), initiative_id),
    )
    conn.commit()


def _clear_thread(conn: sqlite3.Connection, initiative_id: str) -> None:
    conn.execute(
        """
        UPDATE mirror_initiatives
        SET thread_id = NULL, starter_message_id = NULL, published_hash = NULL, updated_at = ?
        WHERE id = ?
        """,
        (int(time.time()), initiative_id),
    )
    conn.commit()


def _is_discord_not_found(exc: Exception) -> bool:
    return isinstance(exc, DiscordAPIError) and exc.status == 404


def _missing_members(initiative: Initiative, snapshot: BoardSnapshot) -> list[str]:
    """Member task_ids that no longer resolve to any card in the snapshot.

    Mandatory safety guard: the planner computes "all members terminal" only
    over member cards it can still find in the snapshot, so a task deleted
    outright from the board (not completed — gone) would otherwise look
    indistinguishable from "done" and get silently archived. We refuse to
    archive/close out an initiative while any member is unaccounted for.
    """
    return sorted(tid for tid in initiative.members if tid not in snapshot.cards)


async def _resolve_tag_ids(client: DiscordClient, cfg: MirrorConfig, tags: list[str]) -> list[str]:
    channel = await asyncio.to_thread(client.get_channel, cfg.forum_channel_id)
    lookup, _created = await asyncio.to_thread(ensure_forum_tags, client, channel, tags)
    return [lookup[t.strip().lower()] for t in tags if t.strip().lower() in lookup]


async def _call_with_archive_retry(
    client: DiscordClient, thread_id: str, active: bool, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Run a sync DiscordClient call in a thread.

    Historical auto-unarchive behavior is intentionally disabled for the Kanban
    mirror. Closed/archived/locked targets are now handled only by the
    config-driven closed-thread reply policy.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


def _origin_header(cfg: MirrorConfig, initiative: Initiative, *, source: str, task_id: str | None = None) -> str:
    primary_task = task_id or next(iter(initiative.members), initiative.id)
    return (
        "Origin: Hermes Kanban Discord mirror "
        f"board={cfg.board} card={primary_task} initiative={initiative.id} "
        f"source={source} original_thread={initiative.thread_id or 'missing'}"
    )


async def _thread_state(client: DiscordClient, thread_id: str | None) -> tuple[str, dict[str, Any] | None]:
    if not thread_id:
        return "missing", None
    try:
        channel = await asyncio.to_thread(client.get_channel, thread_id)
    except DiscordAPIError as exc:
        if exc.status == 404:
            return "missing", None
        raise
    return classify_thread_state(channel), channel


async def _send_redirect(
    client: DiscordClient,
    destination: dict[str, str],
    content: str,
) -> str:
    platform = destination.get("platform")
    kind = destination.get("kind")
    if platform != "discord" or kind != "dm":
        raise RuntimeError(f"unsupported closed-thread redirect destination: {platform}/{kind}")
    user_id = destination.get("user_id")
    if not user_id:
        raise RuntimeError("closed-thread redirect destination missing user_id")
    dm = await asyncio.to_thread(client.create_dm, user_id)
    channel_id = str(dm.get("id") or "")
    if not channel_id:
        raise RuntimeError("Discord DM creation response did not include a channel id")
    message_id = ""
    for chunk in split_discord_message(content):
        resp = await asyncio.to_thread(client.send_message, channel_id, content=chunk)
        message_id = str(resp.get("id") or message_id)
    return message_id


async def _send_with_closed_thread_policy(
    cfg: MirrorConfig,
    client: DiscordClient,
    initiative: Initiative,
    *,
    source: str,
    content: str,
    task_id: str | None = None,
    log: list[str] | None = None,
) -> tuple[bool, str, str]:
    """Send a reply to an initiative thread or route it per closed-thread policy.

    Returns ``(handled, action, message_id)``. ``handled`` means the caller may
    advance mirror progress / record the note. Failed redirect/reopen attempts
    return ``False`` so the note is retried later, but they never fall back to
    posting into the original closed thread.
    """
    thread_id = initiative.thread_id
    state, _channel = await _thread_state(client, thread_id)
    if state == "active" and thread_id:
        message_id = ""
        for chunk in split_discord_message(content):
            resp = await asyncio.to_thread(client.send_message, thread_id, content=chunk)
            message_id = str(resp.get("id") or message_id)
        return True, "sent", message_id

    context = {
        "board": cfg.board,
        "forum_channel_id": cfg.forum_channel_id,
        "task_id": task_id,
        "initiative_id": initiative.id,
        "thread_id": thread_id,
        "thread_state": state,
        "source": source,
    }
    action, destination = resolve_closed_thread_action(cfg.closed_thread_reply_policy, context)
    prefix = f"closed_thread_policy: {initiative.id} thread={thread_id or 'missing'} state={state} source={source} action={action}"

    if action == "discard":
        if log is not None:
            log.append(prefix)
        return True, "discard", ""

    if action == "redirect":
        try:
            message_id = await _send_redirect(client, destination or {}, f"{_origin_header(cfg, initiative, source=source, task_id=task_id)}\n\n{content}")
        except Exception as exc:
            logger.warning("kanban mirror: %s redirect failed: %s", prefix, exc)
            if log is not None:
                log.append(f"{prefix} redirect_failed")
            await _comment_closed_thread_failure_if_configured(
                cfg,
                initiative,
                "redirect_failure",
                f"Discord mirror redirect failed for closed thread {thread_id or 'missing'}: {exc}",
            )
            return False, "redirect_error", ""
        if log is not None:
            log.append(prefix)
        return True, "redirect", message_id

    if action == "reopen_thread":
        if state == "missing" or not thread_id:
            logger.warning("kanban mirror: %s cannot reopen missing thread", prefix)
            if log is not None:
                log.append(f"{prefix} reopen_failed")
            await _comment_reopen_failure_if_configured(
                cfg,
                initiative,
                "Discord mirror reopen failed because the configured thread is missing.",
            )
            return False, "reopen_error", ""
        try:
            await asyncio.to_thread(client.update_thread, thread_id, archive=False, locked=False)
            reopened_state, _ = await _thread_state(client, thread_id)
            if reopened_state != "active":
                raise RuntimeError(f"thread remained {reopened_state} after reopen")
            message_id = ""
            for chunk in split_discord_message(content):
                resp = await asyncio.to_thread(client.send_message, thread_id, content=chunk)
                message_id = str(resp.get("id") or message_id)
        except Exception as exc:
            logger.warning("kanban mirror: %s reopen failed: %s", prefix, exc)
            if log is not None:
                log.append(f"{prefix} reopen_failed")
            await _comment_reopen_failure_if_configured(
                cfg,
                initiative,
                f"Discord mirror reopen failed for closed thread {thread_id}: {exc}",
            )
            return False, "reopen_error", ""
        if log is not None:
            log.append(prefix)
        return True, "reopen_thread", message_id

    logger.warning("kanban mirror: %s unsupported action; discarding", prefix)
    if log is not None:
        log.append(f"{prefix} unsupported_action_discarded")
    return True, "discard", ""


async def _publish_edit(client: DiscordClient, cfg: MirrorConfig, initiative: Initiative,
                         title: str, body: str, tags: list[str]) -> bool:
    if not initiative.thread_id:
        return False
    state, _ = await _thread_state(client, initiative.thread_id)
    if state != "active":
        action, destination = resolve_closed_thread_action(
            cfg.closed_thread_reply_policy,
            {
                "board": cfg.board,
                "forum_channel_id": cfg.forum_channel_id,
                "initiative_id": initiative.id,
                "thread_id": initiative.thread_id,
                "thread_state": state,
                "source": "post_edit",
            },
        )
        prefix = f"closed_thread_policy: {initiative.id} thread={initiative.thread_id} state={state} source=post_edit action={action}"
        if action == "discard":
            logger.info("kanban mirror: %s", prefix)
            return False
        if action == "redirect":
            try:
                await _send_redirect(client, destination or {}, f"{_origin_header(cfg, initiative, source='post_edit')}\n\n{body}")
            except Exception as exc:
                await _comment_closed_thread_failure_if_configured(
                    cfg,
                    initiative,
                    "redirect_failure",
                    f"Discord mirror redirect failed for closed thread {initiative.thread_id}: {exc}",
                )
                raise
            logger.info("kanban mirror: %s", prefix)
            return True
        if action == "reopen_thread":
            try:
                await asyncio.to_thread(client.update_thread, initiative.thread_id, archive=False, locked=False)
                reopened_state, _ = await _thread_state(client, initiative.thread_id)
                if reopened_state != "active":
                    raise RuntimeError(f"thread remained {reopened_state} after reopen")
                logger.info("kanban mirror: %s", prefix)
            except Exception as exc:
                await _comment_reopen_failure_if_configured(
                    cfg,
                    initiative,
                    f"Discord mirror reopen failed for closed thread {initiative.thread_id}: {exc}",
                )
                raise
        else:
            return False
    active = initiative.archived_at is None
    tag_ids = await _resolve_tag_ids(client, cfg, tags)
    await _call_with_archive_retry(
        client, initiative.thread_id, active, client.update_message,
        initiative.thread_id, initiative.starter_message_id, content=body,
    )
    await _call_with_archive_retry(
        client, initiative.thread_id, active, client.update_thread,
        initiative.thread_id, name=title, tag_ids=tag_ids,
    )
    return True


def _read_legacy_rows(board: str) -> list[sqlite3.Row]:
    """Read v1's ``discord_forum_mirror`` table from kanban.db, strictly read-only."""
    from hermes_cli.kanban_db import kanban_db_path

    path = kanban_db_path(board)
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=0", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        try:
            return list(conn.execute("SELECT * FROM discord_forum_mirror"))
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# op executors
# ---------------------------------------------------------------------------


async def _do_curate(cfg: MirrorConfig, client: DiscordClient | None, conn: sqlite3.Connection,
                      snapshot: BoardSnapshot, state: dict[str, Initiative], op: Op,
                      dry_run: bool, allow_llm: bool, log: list[str]) -> None:
    task_ids = op.data["task_ids"]
    unassigned_roots = [snapshot.cards[t] for t in task_ids if t in snapshot.cards]
    decisions = None
    if allow_llm:
        try:
            # Archived initiatives are excluded from what the LLM can "join"
            # into — writer.curate's own validation also rejects a join
            # targeting an id that isn't in this dict, but filtering here
            # keeps the LLM from ever being offered a dead initiative.
            active_state = {k: v for k, v in state.items() if v.archived_at is None}
            decisions = await writer.curate(unassigned_roots, active_state, snapshot)
        except WriterError as exc:
            logger.warning("kanban mirror: curate failed (%s); falling back to 1:1 initiatives", exc)
            decisions = None

    if decisions is None:
        for card in unassigned_roots:
            initiative_id = f"init_{card.id}"
            title = redact(str(card.title or card.id))
            log.append(f"curate: fallback own_post {card.id} -> {initiative_id} ({title!r})")
            if not dry_run:
                await asyncio.to_thread(create_initiative, conn, initiative_id, title)
                await asyncio.to_thread(add_member, conn, initiative_id, card.id)
        return

    # own_post decisions are created before join decisions are applied so
    # that a "join" targeting a batch-internal `init_<task_id>` (validated
    # by writer.curate against the same batch) resolves to a row that
    # already exists by the time add_member runs.
    for decision in decisions:
        if decision.action != "own_post":
            continue
        initiative_id = f"init_{decision.task_id}"
        title = redact(decision.title or "")
        log.append(f"curate: own_post {decision.task_id} -> {initiative_id} ({title!r})")
        if not dry_run:
            await asyncio.to_thread(create_initiative, conn, initiative_id, title)
            await asyncio.to_thread(add_member, conn, initiative_id, decision.task_id)

    for decision in decisions:
        if decision.action != "join":
            continue
        log.append(f"curate: join {decision.task_id} -> {decision.initiative_id}")
        if not dry_run:
            await asyncio.to_thread(add_member, conn, decision.initiative_id, decision.task_id)

    for decision in decisions:
        if decision.action != "digest":
            continue
        log.append(f"curate: digest {decision.task_id}")
        if not dry_run:
            existing = await asyncio.to_thread(get_digest, conn)
            if existing is None:
                await asyncio.to_thread(create_initiative, conn, "digest", cfg.digest_title, "digest")
            await asyncio.to_thread(add_member, conn, "digest", decision.task_id)


async def _do_create_thread(cfg: MirrorConfig, client: DiscordClient | None, conn: sqlite3.Connection,
                             snapshot: BoardSnapshot, state: dict[str, Initiative], op: Op,
                             dry_run: bool, log: list[str]) -> None:
    data = op.data
    initiative_id, title, body, tags = data["initiative_id"], data["title"], data["body"], data["tags"]
    initiative = state.get(initiative_id)
    member_cards = [snapshot.cards[task_id] for task_id in initiative.members if task_id in snapshot.cards] if initiative is not None else []
    attachments = [path for path in review_artifact_paths(member_cards, snapshot) if Path(path).is_file()]
    log.append(f"create_thread: {initiative_id} {title!r} tags={tags}")
    if attachments:
        log.append(f"create_thread attachments: {attachments}")
    if dry_run or client is None:
        return
    tag_ids = await _resolve_tag_ids(client, cfg, tags)
    try:
        resp = await asyncio.to_thread(
            client.create_forum_thread, cfg.forum_channel_id, name=title, content=body, tag_ids=tag_ids,
            attachments=attachments,
        )
    except Exception as exc:
        if attachments:
            logger.warning(
                "kanban mirror: create_thread with attachments failed for %s (%s); retrying without files",
                initiative_id, exc,
            )
            try:
                resp = await asyncio.to_thread(
                    client.create_forum_thread, cfg.forum_channel_id, name=title, content=body, tag_ids=tag_ids,
                )
            except Exception as retry_exc:
                logger.warning("kanban mirror: create_thread failed for %s: %s", initiative_id, retry_exc)
                return
        else:
            logger.warning("kanban mirror: create_thread failed for %s: %s", initiative_id, exc)
            return
    thread_id = str(resp.get("id") or "")
    starter_id = str((resp.get("message") or {}).get("id") or thread_id)
    await asyncio.to_thread(set_thread, conn, initiative_id, thread_id, starter_id)
    initiative = state.get(initiative_id)
    if initiative is not None:
        h = current_publish_hash(initiative, snapshot, cfg)
        await asyncio.to_thread(_store_published_hash, conn, initiative_id, h)


async def _do_edit_post(cfg: MirrorConfig, client: DiscordClient | None, conn: sqlite3.Connection,
                         snapshot: BoardSnapshot, state: dict[str, Initiative], op: Op,
                         dry_run: bool, log: list[str]) -> None:
    data = op.data
    initiative_id, title, body, tags = data["initiative_id"], data["title"], data["body"], data["tags"]
    initiative = state.get(initiative_id)
    log.append(f"edit_post: {initiative_id} {title!r} tags={tags}")
    if dry_run or client is None or initiative is None or not initiative.thread_id:
        return
    try:
        await _publish_edit(client, cfg, initiative, title, body, tags)
    except Exception as exc:
        if _is_discord_not_found(exc):
            logger.warning(
                "kanban mirror: edit_post target missing for %s; clearing stale thread mapping: %s",
                initiative_id,
                exc,
            )
            log.append(f"edit_post: CLEARED stale thread mapping for {initiative_id}")
            await asyncio.to_thread(_clear_thread, conn, initiative_id)
            return
        logger.warning("kanban mirror: edit_post failed for %s: %s", initiative_id, exc)
        return
    h = current_publish_hash(initiative, snapshot, cfg)
    await asyncio.to_thread(_store_published_hash, conn, initiative_id, h)


async def _do_post_note(cfg: MirrorConfig, client: DiscordClient | None, conn: sqlite3.Connection,
                         snapshot: BoardSnapshot, state: dict[str, Initiative], op: Op,
                         dry_run: bool, allow_llm: bool, log: list[str]) -> None:
    data = op.data
    initiative_id, note_key = data["initiative_id"], data["note_key"]
    note_kind, task_id = data["note_kind"], data["task_id"]
    initiative = state.get(initiative_id)
    if initiative is None:
        return

    if note_kind == "initiative_done":
        missing = _missing_members(initiative, snapshot)
        if missing:
            logger.warning(
                "kanban mirror: skipping initiative_done note for %s; member(s) missing from board: %s",
                initiative_id, missing,
            )
            log.append(f"post_note: SKIPPED {note_key} (missing members {missing})")
            return

    if allow_llm:
        try:
            text = await writer.write_note(initiative, snapshot, note_kind, task_id, cfg.note_char_limit)
        except WriterError as exc:
            logger.warning("kanban mirror: write_note failed for %s: %s", note_key, exc)
            log.append(f"post_note: SKIPPED {note_key} (writer error: {exc})")
            return
    else:
        card = snapshot.cards.get(task_id)
        text = f"[{note_kind}] {card.title if card is not None else task_id}"

    text = redact(text)
    log.append(f"post_note: {initiative_id} [{note_kind}] {text!r}")
    if dry_run or client is None:
        return

    message_id = ""
    try:
        handled, action, message_id = await _send_with_closed_thread_policy(
            cfg,
            client,
            initiative,
            source="live_reply",
            content=text,
            task_id=task_id,
            log=log,
        )
        if not handled:
            return
        if action == "discard":
            message_id = f"discarded:{note_key}"
    except Exception as exc:
        logger.warning("kanban mirror: post_note failed for %s: %s", note_key, exc)
        return
    await asyncio.to_thread(record_note, conn, initiative_id, note_key, message_id)


async def _latest_thread_activity_ts(client: DiscordClient, thread_id: str) -> float | None:
    """Return the latest message timestamp for a Discord thread, if known."""
    channel = await asyncio.to_thread(client.get_channel, thread_id)
    message_id = str(channel.get("last_message_id") or "").strip()
    if not message_id:
        return None
    message = await asyncio.to_thread(client.get_message, thread_id, message_id)
    raw_ts = str(message.get("timestamp") or "").strip()
    if not raw_ts:
        return None
    try:
        dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


async def _done_thread_idle_seconds(client: DiscordClient, thread_id: str, now: float) -> float | None:
    latest_ts = await _latest_thread_activity_ts(client, thread_id)
    if latest_ts is None:
        return None
    return max(0.0, now - latest_ts)


async def _do_archive_thread(cfg: MirrorConfig, client: DiscordClient | None, conn: sqlite3.Connection,
                              snapshot: BoardSnapshot, state: dict[str, Initiative], op: Op,
                              dry_run: bool, log: list[str]) -> None:
    initiative_id = op.data["initiative_id"]
    initiative = state.get(initiative_id)
    if initiative is None:
        return
    missing = _missing_members(initiative, snapshot)
    if missing:
        logger.warning(
            "kanban mirror: skipping archive for %s; member(s) missing from board: %s", initiative_id, missing,
        )
        log.append(f"archive_thread: SKIPPED {initiative_id} (missing members {missing})")
        return

    idle_delay_seconds = max(0.0, float(cfg.done_thread_archive_idle_minutes) * 60.0)
    if initiative.thread_id and client is not None and idle_delay_seconds > 0:
        try:
            idle_seconds = await _done_thread_idle_seconds(client, initiative.thread_id, time.time())
        except Exception as exc:
            logger.warning("kanban mirror: archive idle check failed for %s: %s", initiative_id, exc)
            log.append(f"archive_thread: SKIPPED {initiative_id} (idle check failed)")
            return
        if idle_seconds is None:
            logger.info("kanban mirror: skipping archive for %s; latest thread activity unknown", initiative_id)
            log.append(f"archive_thread: SKIPPED {initiative_id} (latest activity unknown)")
            return
        if idle_seconds < idle_delay_seconds:
            remaining = int(idle_delay_seconds - idle_seconds)
            log.append(f"archive_thread: SKIPPED {initiative_id} (idle {int(idle_seconds)}s < required {int(idle_delay_seconds)}s; retry in {remaining}s)")
            return

    log.append(f"archive_thread: {initiative_id}")
    if dry_run or client is None:
        return
    if initiative.thread_id:
        try:
            await asyncio.to_thread(client.update_thread, initiative.thread_id, archive=True)
        except Exception as exc:
            logger.warning("kanban mirror: archive_thread failed for %s: %s", initiative_id, exc)
            return
    await asyncio.to_thread(set_archived, conn, initiative_id, int(time.time()))


async def _do_ensure_digest(cfg: MirrorConfig, client: DiscordClient | None, conn: sqlite3.Connection,
                             op: Op, dry_run: bool, log: list[str]) -> None:
    title, body = op.data["title"], op.data["body"]
    log.append(f"ensure_digest: {title!r}")
    if dry_run or client is None:
        return
    digest = await asyncio.to_thread(get_digest, conn)
    h = _digest_hash(title, body)
    if digest is None or not digest.thread_id:
        try:
            resp = await asyncio.to_thread(
                client.create_forum_thread, cfg.forum_channel_id, name=title, content=body, tag_ids=[],
            )
        except Exception as exc:
            logger.warning("kanban mirror: ensure_digest create failed: %s", exc)
            return
        thread_id = str(resp.get("id") or "")
        starter_id = str((resp.get("message") or {}).get("id") or thread_id)
        if digest is None:
            await asyncio.to_thread(create_initiative, conn, "digest", title, "digest")
        await asyncio.to_thread(set_thread, conn, "digest", thread_id, starter_id)
        try:
            await asyncio.to_thread(client.update_thread, thread_id, pinned=True)
        except Exception as exc:
            logger.warning("kanban mirror: failed to pin digest thread %s: %s", thread_id, exc)
        await asyncio.to_thread(_store_published_hash, conn, "digest", h)
    else:
        active = digest.archived_at is None
        try:
            await _call_with_archive_retry(
                client, digest.thread_id, active, client.update_message,
                digest.thread_id, digest.starter_message_id, content=body,
            )
            await _call_with_archive_retry(
                client, digest.thread_id, active, client.update_thread, digest.thread_id, name=title,
            )
        except Exception as exc:
            if _is_discord_not_found(exc):
                logger.warning("kanban mirror: digest edit target missing; clearing stale thread mapping: %s", exc)
                log.append("ensure_digest: CLEARED stale thread mapping")
                await asyncio.to_thread(_clear_thread, conn, "digest")
                return
            logger.warning("kanban mirror: ensure_digest edit failed: %s", exc)
            return
        await asyncio.to_thread(_store_published_hash, conn, "digest", h)


async def _do_mark_stale(conn: sqlite3.Connection, op: Op, dry_run: bool, log: list[str]) -> None:
    initiative_id = op.data["initiative_id"]
    log.append(f"mark_stale: {initiative_id}")
    if not dry_run:
        await asyncio.to_thread(mark_brief_stale, conn, initiative_id)


async def _do_member_seen(conn: sqlite3.Connection, op: Op, dry_run: bool, log: list[str]) -> None:
    task_id, status, sig = op.data["task_id"], op.data["status"], op.data["sig"]
    log.append(f"member_seen: {task_id} -> {status}")
    if not dry_run:
        await asyncio.to_thread(set_member_seen, conn, task_id, status, sig)


# ---------------------------------------------------------------------------
# prose pass
# ---------------------------------------------------------------------------


async def _prose_pass(cfg: MirrorConfig, client: DiscordClient | None, conn: sqlite3.Connection,
                       snapshot: BoardSnapshot, state: dict[str, Initiative], log: list[str]) -> None:
    global _LAST_PROSE_PASS
    now_mono = time.monotonic()
    if now_mono - _LAST_PROSE_PASS < cfg.prose_interval_seconds:
        return
    _LAST_PROSE_PASS = now_mono

    for initiative in state.values():
        if initiative.kind != "post" or initiative.archived_at is not None:
            continue
        if not initiative.brief_stale or not initiative.thread_id:
            continue
        failures, next_try = _PROSE_BACKOFF.get(initiative.id, (0, 0.0))
        if now_mono < next_try:
            continue
        try:
            result = await writer.write_prose(initiative, snapshot)
        except WriterError as exc:
            failures += 1
            delay = min(_PROSE_BACKOFF_BASE * (2 ** (failures - 1)), _PROSE_BACKOFF_CAP)
            _PROSE_BACKOFF[initiative.id] = (failures, now_mono + delay)
            logger.warning(
                "kanban mirror: write_prose failed for %s (attempt %d, retry in %.0fs): %s",
                initiative.id, failures, delay, exc,
            )
            continue
        _PROSE_BACKOFF.pop(initiative.id, None)
        await asyncio.to_thread(set_prose, conn, initiative.id, result.brief, result.needs_you or "", result.blocked_reasons)

        now = int(time.time())
        refreshed = Initiative(
            id=initiative.id, title=initiative.title, kind=initiative.kind,
            thread_id=initiative.thread_id, starter_message_id=initiative.starter_message_id,
            brief=result.brief, needs_you=result.needs_you, blocked_reasons=result.blocked_reasons,
            published_hash=initiative.published_hash, brief_stale=False, brief_updated_at=now,
            archived_at=initiative.archived_at, created_at=initiative.created_at, updated_at=now,
            members=initiative.members,
        )
        new_hash = current_publish_hash(refreshed, snapshot, cfg)
        log.append(f"prose: {initiative.id} refreshed")
        if new_hash == initiative.published_hash:
            continue
        title = post_title(refreshed, snapshot)
        body = render_post(refreshed, snapshot, cfg.max_post_chars, now)
        tags = _tags_for(refreshed, snapshot)
        log.append(f"edit_post(prose): {initiative.id} {title!r}")
        if client is None:
            continue
        try:
            await _publish_edit(client, cfg, refreshed, title, body, tags)
        except Exception as exc:
            if _is_discord_not_found(exc):
                logger.warning(
                    "kanban mirror: prose edit_post target missing for %s; clearing stale thread mapping: %s",
                    initiative.id,
                    exc,
                )
                log.append(f"edit_post(prose): CLEARED stale thread mapping for {initiative.id}")
                await asyncio.to_thread(_clear_thread, conn, initiative.id)
                continue
            logger.warning("kanban mirror: prose edit_post failed for %s: %s", initiative.id, exc)
            continue
        await asyncio.to_thread(_store_published_hash, conn, initiative.id, new_hash)


async def _comment_closed_thread_failure_if_configured(
    cfg: MirrorConfig,
    initiative: Initiative,
    failure_key: str,
    message: str,
) -> None:
    if cfg.closed_thread_reply_policy.failure_policy.get(failure_key) != "log_and_kanban_comment":
        return
    task_id = next(iter(initiative.members), None)
    if not task_id:
        return
    try:
        from hermes_cli import kanban_db as _kb

        def _write() -> None:
            conn = _kb.connect(board=cfg.board)
            try:
                _kb.add_comment(conn, task_id, "ops", message)
            finally:
                conn.close()

        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warning("kanban mirror: failed to record %s on Kanban card %s: %s", failure_key, task_id, exc)


async def _comment_reopen_failure_if_configured(
    cfg: MirrorConfig,
    initiative: Initiative,
    message: str,
) -> None:
    await _comment_closed_thread_failure_if_configured(cfg, initiative, "reopen_failure", message)


def _append_closed_thread_policy_counts(log: list[str]) -> None:
    counts = {
        "discarded_closed_thread": 0,
        "redirected_closed_thread": 0,
        "reopened_closed_thread": 0,
        "closed_thread_policy_errors": 0,
    }
    for line in log:
        if not line.startswith("closed_thread_policy:"):
            continue
        if "redirect_failed" in line or "reopen_failed" in line:
            counts["closed_thread_policy_errors"] += 1
        elif "action=discard" in line:
            counts["discarded_closed_thread"] += 1
        elif "action=redirect" in line:
            counts["redirected_closed_thread"] += 1
        elif "action=reopen_thread" in line:
            counts["reopened_closed_thread"] += 1
    if any(counts.values()):
        log.append("closed_thread_policy_counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


# ---------------------------------------------------------------------------
# tick / reconcile / rebuild / daemon entry
# ---------------------------------------------------------------------------


async def tick(cfg: MirrorConfig, client: DiscordClient | None, mirror_conn: sqlite3.Connection, *,
               dry_run: bool = False, allow_llm: bool = True) -> list[str]:
    log: list[str] = []
    try:
        snapshot = await asyncio.to_thread(load_board_snapshot, cfg.board)
    except sqlite3.OperationalError as exc:
        logger.warning("kanban mirror: board snapshot unavailable (locked/busy?): %s", exc)
        return log

    state = await asyncio.to_thread(load_mirror_state, mirror_conn)
    digest = await asyncio.to_thread(get_digest, mirror_conn)
    note_keys = await asyncio.to_thread(load_note_keys, mirror_conn)
    now = int(time.time())
    ops = plan(snapshot, state, digest, note_keys, cfg, now)

    for op in ops:
        try:
            if op.kind == "curate":
                await _do_curate(cfg, client, mirror_conn, snapshot, state, op, dry_run, allow_llm, log)
            elif op.kind == "create_thread":
                await _do_create_thread(cfg, client, mirror_conn, snapshot, state, op, dry_run, log)
            elif op.kind == "edit_post":
                await _do_edit_post(cfg, client, mirror_conn, snapshot, state, op, dry_run, log)
            elif op.kind == "post_note":
                await _do_post_note(cfg, client, mirror_conn, snapshot, state, op, dry_run, allow_llm, log)
            elif op.kind == "archive_thread":
                await _do_archive_thread(cfg, client, mirror_conn, snapshot, state, op, dry_run, log)
            elif op.kind == "ensure_digest":
                await _do_ensure_digest(cfg, client, mirror_conn, op, dry_run, log)
            elif op.kind == "mark_stale":
                await _do_mark_stale(mirror_conn, op, dry_run, log)
            elif op.kind == "member_seen":
                await _do_member_seen(mirror_conn, op, dry_run, log)
            else:
                logger.warning("kanban mirror: unknown op kind %r", op.kind)
        except Exception:
            # One broken op must not abort the rest of this tick's plan —
            # log and continue with the remaining ops (the failed op's state
            # stays unadvanced, so it retries next tick).
            logger.exception("kanban mirror: op %s failed; continuing with remaining ops", op.kind)

    if allow_llm and not dry_run:
        await _prose_pass(cfg, client, mirror_conn, snapshot, state, log)

    _append_closed_thread_policy_counts(log)
    return log


async def reconcile(cfg: MirrorConfig, client: DiscordClient, conn: sqlite3.Connection) -> None:
    """Startup drift repair. Wrapped so a failure here never kills the daemon."""
    try:
        state = await asyncio.to_thread(load_mirror_state, conn)
        for initiative in state.values():
            if not initiative.thread_id:
                continue
            try:
                channel = await asyncio.to_thread(client.get_channel, initiative.thread_id)
            except DiscordAPIError as exc:
                if exc.status == 404:
                    logger.info(
                        "kanban mirror: thread %s for %s gone (404); clearing mapping for recreation",
                        initiative.thread_id, initiative.id,
                    )
                    await asyncio.to_thread(_clear_thread, conn, initiative.id)
                else:
                    logger.warning("kanban mirror: reconcile get_channel failed for %s: %s", initiative.id, exc)
                continue
            except Exception as exc:
                logger.warning("kanban mirror: reconcile get_channel error for %s: %s", initiative.id, exc)
                continue

            thread_state = classify_thread_state(channel)
            if thread_state != "active" and initiative.archived_at is None:
                action, _destination = resolve_closed_thread_action(
                    cfg.closed_thread_reply_policy,
                    {
                        "board": cfg.board,
                        "forum_channel_id": cfg.forum_channel_id,
                        "initiative_id": initiative.id,
                        "thread_id": initiative.thread_id,
                        "thread_state": thread_state,
                        "source": "reconcile",
                    },
                )
                if action != "reopen_thread":
                    logger.info(
                        "kanban mirror: reconcile left %s thread %s for active initiative %s closed by policy action=%s",
                        thread_state, initiative.thread_id, initiative.id, action,
                    )
                    continue
                try:
                    await asyncio.to_thread(client.update_thread, initiative.thread_id, archive=False, locked=False)
                    reopened_state, _ = await _thread_state(client, initiative.thread_id)
                    if reopened_state != "active":
                        raise RuntimeError(f"thread remained {reopened_state} after reopen")
                    logger.info(
                        "kanban mirror: reconcile reopened %s thread %s for active initiative %s by policy",
                        thread_state, initiative.thread_id, initiative.id,
                    )
                except Exception as exc:
                    logger.warning("kanban mirror: reconcile failed to reopen %s: %s", initiative.id, exc)
                    await _comment_reopen_failure_if_configured(
                        cfg,
                        initiative,
                        f"Discord mirror reconcile failed to reopen closed thread {initiative.thread_id}: {exc}",
                    )
    except Exception:
        logger.exception("kanban mirror: reconcile failed; continuing without repair")


async def rebuild(cfg: MirrorConfig, client: DiscordClient | None, mirror_conn: sqlite3.Connection, *,
                   dry_run: bool, adopt_legacy: bool = False) -> list[str]:
    log: list[str] = []
    snapshot = await asyncio.to_thread(load_board_snapshot, cfg.board)
    roots = snapshot.active_roots()
    log.append(f"rebuild: {len(roots)} active root(s) on board {cfg.board!r}")

    decisions = None
    if roots:
        try:
            decisions = await writer.curate(roots, {}, snapshot)
        except WriterError as exc:
            logger.warning("kanban mirror: rebuild curate failed (%s); using 1:1 fallback", exc)
            decisions = None

    legacy_rows = await asyncio.to_thread(_read_legacy_rows, cfg.board) if adopt_legacy else []
    legacy_by_task = {str(r["task_id"]): r for r in legacy_rows}

    # groups: initiative_id -> {"title", "members": [task_id...], "kind"}
    groups: dict[str, dict] = {}
    if decisions is None:
        for card in roots:
            groups[f"init_{card.id}"] = {
                "title": redact(str(card.title or card.id)), "members": [card.id], "kind": "post",
            }
    else:
        for d in decisions:
            if d.action == "own_post":
                groups[f"init_{d.task_id}"] = {"title": redact(d.title or ""), "members": [d.task_id], "kind": "post"}
        for d in decisions:
            if d.action == "join":
                groups.setdefault(d.initiative_id, {"title": d.initiative_id, "members": [], "kind": "post"})
                groups[d.initiative_id]["members"].append(d.task_id)
        digest_members = [d.task_id for d in decisions if d.action == "digest"]
        if digest_members:
            groups["digest"] = {"title": cfg.digest_title, "members": digest_members, "kind": "digest"}

    # Adoption rule: a legacy thread whose task_id becomes a single-root
    # initiative reuses that thread; everything else gets archived below.
    adopted: dict[str, sqlite3.Row] = {}
    for initiative_id, g in groups.items():
        if g["kind"] != "post" or len(g["members"]) != 1:
            continue
        legacy = legacy_by_task.get(g["members"][0])
        if legacy is not None:
            adopted[initiative_id] = legacy

    log.append(f"curate: {len(groups)} initiative(s) ({'LLM' if decisions is not None else 'fallback 1:1'})")
    for initiative_id, g in sorted(groups.items()):
        note = " [digest]" if g["kind"] == "digest" else ""
        adopt_note = f" (adopts legacy thread {adopted[initiative_id]['thread_id']})" if initiative_id in adopted else ""
        log.append(f"  {initiative_id}: {g['title']!r} <- {g['members']}{note}{adopt_note}")

    now = int(time.time())
    fake_initiatives: dict[str, Initiative] = {}
    for initiative_id, g in groups.items():
        fake_initiatives[initiative_id] = Initiative(
            id=initiative_id, title=g["title"], kind=g["kind"], thread_id=None, starter_message_id=None,
            brief=None, needs_you=None, blocked_reasons={}, published_hash=None, brief_stale=True,
            brief_updated_at=None, archived_at=None, created_at=now, updated_at=now,
            members={m: MemberState(m, None, None) for m in g["members"]},
        )
    for initiative_id, g in sorted(groups.items()):
        if initiative_id in adopted:
            log.append(f"create_thread: SKIPPED {initiative_id} (adopts legacy thread {adopted[initiative_id]['thread_id']})")
            continue
        initiative = fake_initiatives[initiative_id]
        member_cards = [snapshot.cards[m] for m in g["members"] if m in snapshot.cards]
        if g["kind"] == "digest":
            body = render_digest(member_cards, snapshot, 0, cfg.max_post_chars)
            log.append(f"ensure_digest: {g['title']!r} <- {len(member_cards)} card(s)")
        else:
            title = post_title(initiative, snapshot)
            log.append(f"create_thread: {initiative_id} {title!r} <- {len(member_cards)} card(s)")

    if adopt_legacy:
        adopted_thread_ids = {str(row["thread_id"]) for row in adopted.values()}
        for row in legacy_rows:
            if str(row["thread_id"]) in adopted_thread_ids:
                continue
            verb = "would archive" if dry_run else "archiving"
            log.append(f"legacy: {verb} orphan thread {row['thread_id']} (task {row['task_id']})")

    if dry_run:
        return log

    # --- live: persist groups, adopt/archive legacy threads, create/ensure threads ---
    for initiative_id, g in groups.items():
        await asyncio.to_thread(create_initiative, mirror_conn, initiative_id, g["title"], g["kind"])
        for m in g["members"]:
            await asyncio.to_thread(add_member, mirror_conn, initiative_id, m)

    if adopt_legacy:
        for initiative_id, legacy in adopted.items():
            await asyncio.to_thread(
                set_thread, mirror_conn, initiative_id, str(legacy["thread_id"]),
                str(legacy["starter_message_id"] or ""),
            )
            # adopted initiatives are always single-root "post" kind (see
            # the adoption-eligibility filter above), so publish-hash math
            # (which assumes render_post) always applies here.
            h = current_publish_hash(fake_initiatives[initiative_id], snapshot, cfg)
            await asyncio.to_thread(_store_published_hash, mirror_conn, initiative_id, h)
        adopted_thread_ids = {str(row["thread_id"]) for row in adopted.values()}
        for row in legacy_rows:
            tid = str(row["thread_id"])
            if tid in adopted_thread_ids:
                continue
            if client is not None:
                try:
                    await asyncio.to_thread(client.update_thread, tid, archive=True)
                except Exception as exc:
                    logger.warning("kanban mirror: failed to archive legacy thread %s: %s", tid, exc)

    state = await asyncio.to_thread(load_mirror_state, mirror_conn)
    for initiative_id, g in groups.items():
        if initiative_id in adopted:
            continue
        initiative = state.get(initiative_id)
        if initiative is None or initiative.thread_id is not None:
            continue
        if g["kind"] == "digest":
            body = render_digest(
                [snapshot.cards[m] for m in g["members"] if m in snapshot.cards], snapshot, 0, cfg.max_post_chars,
            )
            op = Op("ensure_digest", {"title": g["title"], "body": body})
            await _do_ensure_digest(cfg, client, mirror_conn, op, dry_run, log)
        else:
            title = post_title(initiative, snapshot)
            body = render_post(initiative, snapshot, cfg.max_post_chars, now)
            tags = _tags_for(initiative, snapshot)
            op = Op("create_thread", {"initiative_id": initiative_id, "title": title, "body": body, "tags": tags})
            await _do_create_thread(cfg, client, mirror_conn, snapshot, state, op, dry_run, log)

    return log


async def run_mirror_daemon(is_running: Callable[[], bool]) -> None:
    from gateway.kanban_mirror.config import load_mirror_config

    cfg = load_mirror_config()
    if not cfg.valid():
        logger.info("kanban mirror: disabled or unconfigured")
        return
    token = load_discord_token(cfg.token_env_path)
    if not token:
        logger.warning("kanban mirror: no DISCORD_BOT_TOKEN at %s; disabled", cfg.token_env_path)
        return
    client = DiscordClient(token)
    conn = connect_mirror(mirror_db_path(cfg.board))
    await reconcile(cfg, client, conn)
    while is_running():
        try:
            await tick(cfg, client, conn)
        except Exception:
            logger.exception("kanban mirror: tick failed")
        await asyncio.sleep(cfg.poll_seconds)
