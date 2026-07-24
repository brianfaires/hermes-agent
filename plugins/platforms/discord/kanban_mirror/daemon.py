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
import hashlib
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from plugins.platforms.discord.kanban_mirror.closed_thread_policy import classify_thread_state, resolve_closed_thread_action
from plugins.platforms.discord.kanban_mirror.config import MirrorConfig
from plugins.platforms.discord.kanban_mirror.discord_client import (
    DiscordAPIError,
    DiscordClient,
    ensure_forum_tags,
    load_discord_token,
    split_discord_message,
)
from plugins.platforms.discord.kanban_mirror.planner import Op, _digest_hash, _tags_for, current_publish_hash, plan
from plugins.platforms.discord.kanban_mirror.render import (
    post_title,
    pointed_card_id,
    redact,
    render_digest,
    render_post,
    review_artifact_paths,
    work_item_ids,
)
from plugins.platforms.discord.kanban_mirror.state import (
    BoardSnapshot,
    Initiative,
    MemberState,
    active_thread_binding,
    add_member,
    backfill_legacy_bindings,
    clear_archived,
    connect_mirror,
    create_initiative,
    get_digest,
    is_terminal,
    load_board_snapshot,
    load_mirror_state,
    load_note_keys,
    mark_brief_stale,
    mirror_db_path,
    record_note,
    resumable_binding_transitions,
    set_archived,
    set_member_seen,
    set_prose,
    set_thread,
)
from plugins.platforms.discord.kanban_mirror import writer
from plugins.platforms.discord.kanban_mirror.transitions import TransitionReceipt, request_binding_transition, run_binding_transition
from plugins.platforms.discord.kanban_mirror.lifecycle import run_terminal_lifecycle
from plugins.platforms.discord.kanban_mirror.lifecycle_discord import DiscordLifecyclePublisher
from plugins.platforms.discord.kanban_mirror.reconciliation import (ExpectedThread, ObservedDigest, ObservedThread,
                                                   reconcile_mirror_state)
from plugins.platforms.discord.kanban_mirror.writer import WriterError

logger = logging.getLogger(__name__)


def _canonical_hash(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _is_quarantined(conn: sqlite3.Connection, thread_id: str | None) -> bool:
    return bool(thread_id and conn.execute(
        "SELECT 1 FROM mirror_thread_quarantine WHERE thread_id=? AND resolved_at IS NULL", (thread_id,)
    ).fetchone())


async def _observe_and_reconcile(cfg: MirrorConfig, client: DiscordClient,
                                 conn: sqlite3.Connection, snapshot: BoardSnapshot,
                                 log: list[str]) -> None:
    """Read live Discord state independently per mapped thread and reconcile it."""
    rows = conn.execute("SELECT thread_id,starter_message_id FROM mirror_initiatives "
                        "WHERE kind='post' AND thread_id IS NOT NULL").fetchall()
    try:
        forum = await asyncio.to_thread(client.get_channel, cfg.forum_channel_id)
        tag_names = {str(t.get("id")): str(t.get("name")) for t in forum.get("available_tags", [])}
    except Exception as exc:
        logger.warning("kanban mirror: reconciliation forum snapshot unavailable: %s", exc)
        log.append("reconciliation: PARTIAL forum")
        tag_names = {}
        forum_complete = False
    else:
        forum_complete = True
    observed: dict[str, ObservedThread] = {}
    for row in rows:
        thread_id, starter_id = str(row["thread_id"]), str(row["starter_message_id"] or "")
        if not forum_complete:
            log.append(f"reconciliation: PARTIAL thread={thread_id}")
            continue
        try:
            channel = await asyncio.to_thread(client.get_channel, thread_id)
            starter = await asyncio.to_thread(client.get_message, thread_id, starter_id)
            tags = tuple(tag_names[x] for x in channel.get("applied_tags", []) if x in tag_names)
            payload = {"title": str(channel.get("name") or ""),
                       "body": str(starter.get("content") or ""), "tags": list(tags)}
            existing: set[str] = set()
            message_ids = [str(r[0]) for r in conn.execute(
                "SELECT transition_message_id FROM mirror_binding_transitions "
                "WHERE thread_id=? AND transition_message_id IS NOT NULL", (thread_id,))]
            for message_id in message_ids:
                try:
                    await asyncio.to_thread(client.get_message, thread_id, message_id)
                    existing.add(message_id)
                except DiscordAPIError as exc:
                    if exc.status != 404:
                        raise
            metadata = channel.get("thread_metadata") or {}
            observed[thread_id] = ObservedThread(
                thread_id, starter_id, _canonical_hash(payload), frozenset(existing), payload["title"], tags,
                bool(metadata.get("archived", channel.get("archived", False))),
            )
        except Exception as exc:
            logger.warning("kanban mirror: reconciliation observation failed for %s: %s", thread_id, exc)
            log.append(f"reconciliation: PARTIAL thread={thread_id}")
    state = load_mirror_state(conn)
    expected: dict[str, ExpectedThread] = {}
    for initiative in state.values():
        if initiative.kind != "post" or not initiative.thread_id:
            continue
        member_cards = [snapshot.cards[task_id] for task_id in initiative.members if task_id in snapshot.cards]
        if len(member_cards) != len(initiative.members):
            continue
        terminal = bool(member_cards) and all(is_terminal(str(card.status or "")) for card in member_cards)
        expected[initiative.thread_id] = ExpectedThread(
            post_title(initiative, snapshot), tuple(_tags_for(initiative, snapshot)), terminal,
        )
    observed_digest = None; digest_complete = True
    digest = get_digest(conn)
    if digest is not None and digest.thread_id and digest.starter_message_id:
        try:
            digest_channel = await asyncio.to_thread(client.get_channel, digest.thread_id)
            digest_message = await asyncio.to_thread(client.get_message, digest.thread_id, digest.starter_message_id)
            observed_digest = ObservedDigest(str(digest.thread_id), str(digest_message.get("content") or ""),
                bool(digest_channel.get("pinned") or (int(digest_channel.get("flags") or 0) & 2)))
        except Exception as exc:
            logger.warning("kanban mirror: reconciliation digest snapshot unavailable: %s", exc)
            log.append("reconciliation: PARTIAL digest"); digest_complete = False
    findings = await asyncio.to_thread(reconcile_mirror_state, conn, observed_threads=observed,
                                       cards=((cfg.board, task_id) for task_id in snapshot.cards),
                                       expected_threads=expected, observed_digest=observed_digest,
                                       digest_observation_complete=digest_complete)
    quarantine_codes = {"binding.open_count", "binding.card_missing", "binding.mapping_missing",
                        "thread.starter_mapping_mismatch", "starter.revision_mismatch",
                        "starter.changed_without_transition_confirmation", "transition.confirmation_missing",
                        "thread.premature_archive", "digest.thread_mismatch",
                        "successor.selection_ambiguous"}
    grouped: dict[str, list] = {}
    for finding in findings:
        if finding.code in quarantine_codes:
            grouped.setdefault(finding.thread_id, []).append(finding)
    for thread_id, conflicts in grouped.items():
        row = conn.execute("SELECT quarantined_at FROM mirror_thread_quarantine "
                           "WHERE thread_id=? AND resolved_at IS NULL", (thread_id,)).fetchone()
        if row is None:
            continue
        quarantined_at = int(row[0])
        if conn.execute("SELECT 1 FROM mirror_repair_notices WHERE thread_id=? AND quarantined_at=?",
                        (thread_id, quarantined_at)).fetchone():
            continue
        identity = hashlib.sha256("|".join(sorted(f.finding_key for f in conflicts)).encode()).hexdigest()
        nonce = hashlib.sha256(f"mirror-repair:{thread_id}:{quarantined_at}".encode()).hexdigest()[:25]
        details = "; ".join(f"{f.code}: {json.dumps(f.evidence, sort_keys=True)}" for f in conflicts)
        content = ("[Mirror repair notice — non-conversational]\nConflict: " + details +
                   "\nSafe action: repair Discord/Kanban state without remapping, archiving, or deleting this thread; "
                   "run a complete scan, then call resolve_thread_quarantine().")
        try:
            response = await asyncio.to_thread(client.send_message, thread_id, content=content, nonce=nonce)
            conn.execute("INSERT OR IGNORE INTO mirror_repair_notices VALUES (?,?,?,?,?,?)",
                         (thread_id, quarantined_at, identity, nonce, str(response.get("id") or ""), int(time.time())))
            conn.commit()
            log.append(f"reconciliation: QUARANTINED thread={thread_id}")
        except Exception as exc:
            logger.warning("kanban mirror: repair notice failed for %s: %s", thread_id, exc)
            log.append(f"reconciliation: NOTICE_FAILED thread={thread_id}")


class DiscordTransitionPublisher:
    """Concrete publisher using Discord's durable nonce de-duplication."""

    def __init__(self, client: DiscordClient, cfg: MirrorConfig, conn: sqlite3.Connection | None = None):
        self.client, self.cfg, self.conn = client, cfg, conn

    def _starter_message_id(self, thread_id: str) -> str:
        if self.conn is None:
            return thread_id
        rows = self.conn.execute(
            "SELECT starter_message_id FROM mirror_initiatives WHERE thread_id=?", (thread_id,)
        ).fetchall()
        if len(rows) != 1 or not str(rows[0][0] or "").strip():
            raise ValueError("thread does not have one starter message mapping")
        return str(rows[0][0])

    def publish_transition(self, thread_id: str, payload: dict, *, operation_key: str) -> TransitionReceipt:
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("transition payload requires non-empty content")
        nonce = hashlib.sha256(operation_key.encode()).hexdigest()[:25]
        response = self.client.send_message(thread_id, content=content, nonce=nonce)
        return TransitionReceipt(str(response.get("id") or ""), thread_id, operation_key, _canonical_hash(payload))

    def update_starter(self, thread_id: str, payload: dict) -> None:
        title, body, tags = payload.get("title"), payload.get("body"), payload.get("tags")
        if not isinstance(title, str) or not isinstance(body, str) or not isinstance(tags, list):
            raise ValueError("starter payload requires title, body, and tags")
        forum = self.client.get_channel(self.cfg.forum_channel_id)
        lookup, _ = ensure_forum_tags(self.client, forum, [str(tag) for tag in tags])
        tag_ids = [lookup[str(tag).strip().lower()] for tag in tags if str(tag).strip().lower() in lookup]
        starter_id = self._starter_message_id(thread_id)
        self.client.update_message(thread_id, starter_id, content=body)
        self.client.update_thread(thread_id, name=title, tag_ids=tag_ids)

    def read_starter(self, thread_id: str) -> dict:
        thread = self.client.get_channel(thread_id)
        message = self.client.get_message(thread_id, self._starter_message_id(thread_id))
        forum = self.client.get_channel(self.cfg.forum_channel_id)
        names = {str(tag.get("id")): str(tag.get("name")) for tag in forum.get("available_tags", [])}
        return {"title": str(thread.get("name") or ""), "body": str(message.get("content") or ""),
                "tags": [names[tag] for tag in thread.get("applied_tags", []) if tag in names]}


def _starter_identity_authorized(conn: sqlite3.Connection, thread_id: str, represented_task_id: str | None) -> bool:
    """Fail closed unless a planner edit still represents the confirmed epoch."""
    binding = active_thread_binding(conn, thread_id)
    return binding is not None and represented_task_id == binding.task_id


async def _recover_binding_transitions(cfg: MirrorConfig, client: DiscordClient | None,
                                       conn: sqlite3.Connection, log: list[str]) -> None:
    if not cfg.binding_transitions_enabled:
        return
    await asyncio.to_thread(backfill_legacy_bindings, conn, cfg.board)
    if client is None:
        return
    publisher = DiscordTransitionPublisher(client, cfg, conn)
    for transition in await asyncio.to_thread(resumable_binding_transitions, conn):
        if cfg.reconciliation_enabled and _is_quarantined(conn, transition.thread_id):
            log.append(f"binding_transition: BLOCKED quarantined thread={transition.thread_id}")
            continue
        await asyncio.to_thread(
            run_binding_transition, conn, publisher, transition_key=transition.transition_key,
            thread_id=transition.thread_id, old_card_metadata=transition.old_card_metadata,
            new_card_metadata=transition.new_card_metadata, transition_payload=transition.transition_payload,
            starter_payload=transition.starter_payload,
        )
        log.append(f"binding_transition: resumed {transition.transition_key}")

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
    if cfg.binding_transitions_enabled:
        represented = pointed_card_id(initiative, snapshot)
        authorized = await asyncio.to_thread(
            _starter_identity_authorized, conn, initiative.thread_id, represented
        )
        if not authorized:
            log.append(f"edit_post: BLOCKED identity replacement for {initiative_id}")
            logger.error("kanban mirror: blocked direct starter identity replacement for %s", initiative_id)
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

    current_hash = current_publish_hash(initiative, snapshot, cfg)
    if current_hash != initiative.published_hash:
        logger.info(
            "kanban mirror: skipping archive for %s; pending Discord publish (%s != %s)",
            initiative_id,
            current_hash,
            initiative.published_hash,
        )
        log.append(f"archive_thread: SKIPPED {initiative_id} (pending publish)")
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


async def _audit_active_threads(cfg: MirrorConfig, client: DiscordClient | None, conn: sqlite3.Connection,
                                snapshot: BoardSnapshot, state: dict[str, Initiative], log: list[str]) -> bool:
    """Detect open Discord forum threads that no longer match unfinished Kanban work.

    Discord can drift from mirror.db: a thread may remain open after a card is
    terminal, an archived initiative may be reopened manually, or duplicate
    bot-authored threads may point at one card. This audit runs every tick,
    keeps the thread with the newest Discord activity, archives duplicates,
    and clears stale local archive markers so the normal planner can repair
    the surviving post. Unrecognized/manual threads remain untouched.

    Returns True when mirror state changed and should be reloaded before planning.
    """
    if client is None or not cfg.guild_id:
        return False
    try:
        threads = await asyncio.to_thread(client.list_active_threads, cfg.guild_id)
    except Exception as exc:
        logger.warning("kanban mirror: active-thread audit failed: %s", exc)
        log.append("active_thread_audit: SKIPPED (Discord fetch failed)")
        return False

    by_thread = {initiative.thread_id: initiative for initiative in state.values() if initiative.thread_id}
    active_thread_ids = {
        str(thread.get("id") or "").strip()
        for thread in threads
        if str(thread.get("parent_id") or "") == cfg.forum_channel_id
    }
    active_threads_by_id = {
        str(thread.get("id") or "").strip(): thread
        for thread in threads
        if str(thread.get("parent_id") or "") == cfg.forum_channel_id
    }
    bot_user_id: str | None = None
    orphan_inspections: dict[str, tuple[Initiative | None, str | None]] = {}
    orphan_threads = [
        thread_id
        for thread_id in active_thread_ids
        if thread_id and thread_id not in by_thread
    ]
    if orphan_threads:
        try:
            current_user = await asyncio.to_thread(client.get_current_user)
            bot_user_id = str(current_user.get("id") or "").strip()
        except Exception as exc:
            logger.warning("kanban mirror: could not resolve bot identity for orphan audit: %s", exc)
        if bot_user_id:
            for orphan_thread_id in orphan_threads:
                try:
                    starter = await asyncio.to_thread(client.get_message, orphan_thread_id, orphan_thread_id)
                except Exception as exc:
                    logger.warning(
                        "kanban mirror: orphan thread inspection failed for %s: %s", orphan_thread_id, exc
                    )
                    orphan_inspections[orphan_thread_id] = (None, None)
                    continue
                author = starter.get("author") if isinstance(starter, dict) else None
                author_id = str((author or {}).get("id") or "").strip() if isinstance(author, dict) else ""
                content = str(starter.get("content") or "") if isinstance(starter, dict) else ""
                explicit_card_ids = re.findall(
                    r"(?im)^\s*card_ID\s*:\s*(t_[0-9a-f]{8}|t[0-9]+)\s*$", content
                )
                referenced_cards = {
                    token
                    for token in (
                        explicit_card_ids
                        or re.findall(r"(?<!\w)(t_[0-9a-f]{8}|t[0-9]+)(?!\w)", content)
                    )
                    if token in snapshot.cards
                }
                candidates = [
                    candidate
                    for candidate in state.values()
                    if candidate.kind == "post"
                    and referenced_cards.intersection(work_item_ids(candidate, snapshot))
                ]
                candidate = candidates[0] if len(candidates) == 1 else None
                referenced_task = pointed_card_id(candidate, snapshot) if candidate is not None else None
                if author_id != bot_user_id or candidate is None:
                    candidate = None
                    referenced_task = None
                orphan_inspections[orphan_thread_id] = (candidate, referenced_task)
    def activity_key(thread_id: str) -> tuple[int, str]:
        thread = active_threads_by_id.get(thread_id, {})
        raw = str(thread.get("last_message_id") or thread_id)
        return (int(raw) if raw.isdigit() else 0, thread_id)

    handled_threads: set[str] = set()
    changed = False
    grouped_orphans: dict[str, list[str]] = {}
    for thread_id, (candidate, _) in orphan_inspections.items():
        if candidate is not None:
            grouped_orphans.setdefault(candidate.id, []).append(thread_id)
    for initiative_id, orphan_ids in grouped_orphans.items():
        candidate = state[initiative_id]
        candidates = list(orphan_ids)
        if candidate.thread_id in active_thread_ids:
            candidates.append(str(candidate.thread_id))
        winner = max(candidates, key=activity_key)
        losers = [thread_id for thread_id in candidates if thread_id != winner]
        archived: list[str] = []
        merge_failed = False
        for loser in losers:
            try:
                await asyncio.to_thread(client.update_thread, loser, archive=True)
                archived.append(loser)
            except Exception as exc:
                logger.warning("kanban mirror: duplicate archive failed for %s: %s", loser, exc)
                merge_failed = True
                break
        if merge_failed:
            continue
        if candidate.thread_id != winner:
            await asyncio.to_thread(set_thread, conn, initiative_id, winner, winner)
        if candidate.archived_at is not None:
            await asyncio.to_thread(clear_archived, conn, initiative_id)
        handled_threads.update(candidates)
        changed = True
        action = "MERGE" if archived else "ADOPT"
        suffix = f" archived={','.join(archived)}" if archived else ""
        log.append(f"active_thread_audit: {action} {initiative_id} winner={winner}{suffix}")

    for thread in threads:
        if str(thread.get("parent_id") or "") != cfg.forum_channel_id:
            continue
        thread_id = str(thread.get("id") or "").strip()
        if not thread_id:
            continue
        if thread_id in handled_threads:
            continue
        initiative = by_thread.get(thread_id)
        if initiative is None:
            logger.info("kanban mirror: active Discord thread %s has no mirror mapping", thread_id)
            log.append(f"active_thread_audit: UNMAPPED {thread_id}")
            continue

        member_cards = [snapshot.cards[tid] for tid in initiative.members if tid in snapshot.cards]
        all_terminal = bool(member_cards) and all(is_terminal(str(card.status or "")) for card in member_cards)
        if all_terminal and initiative.archived_at is not None:
            logger.info(
                "kanban mirror: active Discord thread %s maps to archived terminal initiative %s; reopening local mirror state for repair",
                thread_id,
                initiative.id,
            )
            log.append(f"active_thread_audit: REPAIR {initiative.id} thread={thread_id}")
            changed = True
            await asyncio.to_thread(clear_archived, conn, initiative.id)
    return changed


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
        if cfg.reconciliation_enabled and _is_quarantined(conn, initiative.thread_id):
            continue
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
        if cfg.binding_transitions_enabled:
            represented = pointed_card_id(refreshed, snapshot)
            authorized = await asyncio.to_thread(
                _starter_identity_authorized, conn, refreshed.thread_id, represented
            )
            if not authorized:
                log.append(f"edit_post(prose): BLOCKED identity replacement for {initiative.id}")
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


def _terminal_chain(snapshot: BoardSnapshot, task_id: str) -> list[dict]:
    """Return descendants first and authoritative bound card last."""
    ordered: list[str] = []
    seen: set[str] = set()
    def visit(current: str) -> None:
        if current in seen:
            return
        seen.add(current)
        for child in sorted(snapshot.children.get(current, [])):
            visit(child)
        ordered.append(current)
    visit(task_id)
    return [{"task_id": cid, "title": snapshot.cards[cid].title,
             "status": snapshot.cards[cid].status} for cid in ordered if cid in snapshot.cards]


def _record_successor_finding(conn: sqlite3.Connection, thread: str, binding: str,
                              task: str, evidence: dict) -> None:
    code = "successor.selection_ambiguous"; stamp = int(time.time())
    identity = json.dumps([code, thread, binding, task], separators=(",", ":"))
    key = hashlib.sha256(identity.encode()).hexdigest()
    payload = json.dumps({"thread_id": thread, **evidence}, sort_keys=True, separators=(",", ":"))
    evidence_hash = hashlib.sha256(payload.encode()).hexdigest()
    conn.execute("""INSERT INTO mirror_reconciliation_findings
        (finding_key,severity,code,thread_id,binding_key,task_id,evidence,evidence_hash,first_seen_at,last_seen_at)
        VALUES (?,'error',?,?,?,?,?,?,?,?) ON CONFLICT(finding_key) DO UPDATE SET
        evidence=excluded.evidence,evidence_hash=excluded.evidence_hash,last_seen_at=excluded.last_seen_at,resolved_at=NULL""",
        (key, code, thread, binding, task, payload, evidence_hash, stamp, stamp))
    conn.execute("""INSERT INTO mirror_thread_quarantine(thread_id,needs_repair,quarantined_at,updated_at)
        VALUES (?,1,?,?) ON CONFLICT(thread_id) DO UPDATE SET needs_repair=1,updated_at=excluded.updated_at,
        quarantined_at=CASE WHEN resolved_at IS NOT NULL THEN excluded.quarantined_at ELSE quarantined_at END,
        resolved_at=NULL""", (thread, stamp, stamp))
    conn.commit()


def _card_metadata(board: str, card: Any) -> dict:
    return {"board_slug": board, "task_id": card.id, "title": str(card.title or card.id),
            "status": str(card.status or ""), "owner": str(card.assignee or ""),
            "body": str(card.body or ""), "priority": str(card.priority or "")}


def _reachable_cycle(snapshot: BoardSnapshot, root: str) -> bool:
    visiting: set[str] = set(); visited: set[str] = set()
    def visit(node: str) -> bool:
        if node in visiting: return True
        if node in visited: return False
        visiting.add(node)
        if any(visit(child) for child in snapshot.children.get(node, ())): return True
        visiting.remove(node); visited.add(node); return False
    return visit(root)


async def _initiate_automatic_successors(cfg: MirrorConfig, client: DiscordClient,
                                         conn: sqlite3.Connection, snapshot: BoardSnapshot,
                                         state: dict[str, Initiative], log: list[str]) -> None:
    if not cfg.automatic_successor_enabled:
        return
    publisher = DiscordTransitionPublisher(client, cfg, conn)
    mapped = {str(r[0]): (str(r[1]), str(r[2] or "")) for r in conn.execute(
        "SELECT m.task_id,m.initiative_id,i.thread_id FROM mirror_members m JOIN mirror_initiatives i ON i.id=m.initiative_id")}
    for initiative in state.values():
        if initiative.kind != "post" or not initiative.thread_id or initiative.archived_at is not None:
            continue
        binding = active_thread_binding(conn, initiative.thread_id)
        current = snapshot.cards.get(binding.task_id) if binding else None
        if binding is None or current is None or not is_terminal(str(current.status or "")):
            continue
        children = sorted(set(snapshot.children.get(current.id, ())))
        descendants = [x["task_id"] for x in _terminal_chain(snapshot, current.id)[:-1]
                       if not is_terminal(str(x["status"] or ""))]
        eligible: list[str] = []; rejected: dict[str, list[str]] = {}
        cycle = _reachable_cycle(snapshot, current.id)
        for child_id in children:
            child = snapshot.cards.get(child_id); why: list[str] = []
            if child is None: why.append("card_missing")
            else:
                if is_terminal(str(child.status or "")): why.append("terminal")
                parents = sorted(set(snapshot.parents.get(child_id, ())))
                if current.id not in parents: why.append("edge_inconsistent")
                if any(p not in snapshot.cards or not is_terminal(str(snapshot.cards[p].status or "")) for p in parents):
                    why.append("parents_not_terminal")
                if not str(child.assignee or "").strip(): why.append("owner_missing")
                membership = mapped.get(child_id)
                if membership: why.append("already_mapped" if membership[1] == initiative.thread_id else "cross_thread_mapping")
                if len(initiative.members) != 1 or current.id not in initiative.members: why.append("ambiguous_membership")
            if child is not None and not why: eligible.append(child_id)
            rejected[child_id] = why
        if cycle or (descendants and len(eligible) != 1):
            _record_successor_finding(conn, initiative.thread_id, binding.binding_key, current.id,
                {"direct_children": children, "eligible": eligible, "rejections": rejected,
                 "nonterminal_descendants": sorted(descendants), "cycle": cycle})
            log.append(f"automatic_successor: BLOCKED thread={initiative.thread_id}")
            continue
        if len(eligible) != 1:
            continue
        # A clean scan resolves the machine finding, but quarantine remains
        # latched until explicit operator acknowledgement.  Never mutate the
        # starter/binding in the scan which establishes cleanliness.
        stamp = int(time.time())
        conn.execute("""UPDATE mirror_reconciliation_findings SET resolved_at=?,last_seen_at=?
            WHERE thread_id=? AND code='successor.selection_ambiguous' AND resolved_at IS NULL""",
            (stamp, stamp, initiative.thread_id))
        conn.commit()
        if _is_quarantined(conn, initiative.thread_id):
            log.append(f"automatic_successor: BLOCKED quarantined thread={initiative.thread_id}")
            continue
        successor = snapshot.cards[eligible[0]]
        advanced = Initiative(initiative.id, str(successor.title or successor.id), initiative.kind,
            initiative.thread_id, initiative.starter_message_id, None, None, {}, initiative.published_hash,
            True, None, None, initiative.created_at, int(time.time()),
            {successor.id: MemberState(successor.id, None, None)})
        starter = {"title": post_title(advanced, snapshot),
                   "body": render_post(advanced, snapshot, cfg.max_post_chars, int(time.time())),
                   "tags": list(_tags_for(advanced, snapshot))}
        key = f"auto:{binding.binding_key}:{successor.id}"
        note = {"content": f"Work advanced from **{current.title or current.id}** (`{current.id}`) to **{successor.title or successor.id}** (`{successor.id}`)."}
        await asyncio.to_thread(request_binding_transition, conn, publisher, transition_key=key,
            thread_id=initiative.thread_id, old_card_metadata=_card_metadata(cfg.board, current),
            successor_card_metadata=_card_metadata(cfg.board, successor), transition_payload=note,
            frozen_starter_payload=starter)
        log.append(f"automatic_successor: {current.id} -> {successor.id}")


async def _resume_terminal_lifecycles(cfg: MirrorConfig, client: DiscordClient,
                                      conn: sqlite3.Connection, snapshot: BoardSnapshot,
                                      state: dict[str, Initiative], log: list[str]) -> None:
    publisher = DiscordLifecyclePublisher(client, cfg, conn)
    for initiative in state.values():
        if cfg.reconciliation_enabled and _is_quarantined(conn, initiative.thread_id):
            continue
        if initiative.kind != "post" or not initiative.thread_id or initiative.archived_at is not None:
            continue
        binding = await asyncio.to_thread(active_thread_binding, conn, initiative.thread_id)
        if binding is None:
            continue
        chain = _terminal_chain(snapshot, binding.task_id)
        # An absent card is ambiguity, not completion.
        if not chain or chain[-1]["task_id"] != binding.task_id:
            continue
        # Dependency descendants are continuations, not containment.
        if any(not is_terminal(str(item["status"] or "")) for item in chain[:-1]):
            continue
        try:
            activity = await _latest_thread_activity_ts(client, initiative.thread_id)
            if activity is None:
                log.append(f"terminal_lifecycle: SKIPPED {initiative.id} (latest activity unknown)")
                continue
            outcomes = [{"task_id": cid, "outcome": str(snapshot.cards[cid].result or "completed")}
                        for cid in [x["task_id"] for x in chain] if cid in snapshot.cards]
            starts = [str(snapshot.cards[x["task_id"]].created_at) for x in chain if snapshot.cards[x["task_id"]].created_at]
            ends = [str(snapshot.cards[x["task_id"]].completed_at) for x in chain if snapshot.cards[x["task_id"]].completed_at]
            life = await asyncio.to_thread(
                run_terminal_lifecycle, conn, publisher,
                lifecycle_key=f"terminal:{binding.binding_key}", thread_id=initiative.thread_id,
                card_chain=chain, outcomes=outcomes,
                owners=sorted({str(snapshot.cards[x["task_id"]].assignee) for x in chain if snapshot.cards[x["task_id"]].assignee}),
                date_range={"start": min(starts) if starts else None, "end": max(ends) if ends else None},
                thread_link=f"https://discord.com/channels/{cfg.guild_id}/{initiative.thread_id}",
                idle_seconds=max(0, int(cfg.done_thread_archive_idle_minutes * 60)),
                observed_activity_at=int(activity), clock=lambda: int(time.time()),
            )
            if life is not None:
                log.append(f"terminal_lifecycle: {initiative.id} state={life.state}")
                if life.state == "archived":
                    await asyncio.to_thread(set_archived, conn, initiative.id, int(time.time()))
        except Exception:
            logger.exception("kanban mirror: terminal lifecycle failed closed for %s", initiative.id)
            log.append(f"terminal_lifecycle: FAILED {initiative.id}")


async def tick(cfg: MirrorConfig, client: DiscordClient | None, mirror_conn: sqlite3.Connection, *,
               dry_run: bool = False, allow_llm: bool = True) -> list[str]:
    log: list[str] = []
    try:
        snapshot = await asyncio.to_thread(load_board_snapshot, cfg.board)
    except sqlite3.OperationalError as exc:
        logger.warning("kanban mirror: board snapshot unavailable (locked/busy?): %s", exc)
        return log
    if cfg.reconciliation_enabled and not dry_run and client is not None:
        try:
            await _observe_and_reconcile(cfg, client, mirror_conn, snapshot, log)
        except Exception:
            logger.exception("kanban mirror: live reconciliation failed closed")
            log.append("reconciliation: FAILED")
    if not dry_run:
        try:
            await _recover_binding_transitions(cfg, client, mirror_conn, log)
        except Exception:
            logger.exception("kanban mirror: binding transition recovery failed closed")
            log.append("binding_transition: recovery failed")

    state = await asyncio.to_thread(load_mirror_state, mirror_conn)
    if not dry_run and client is not None and cfg.automatic_successor_enabled:
        try:
            await _initiate_automatic_successors(cfg, client, mirror_conn, snapshot, state, log)
        except Exception:
            logger.exception("kanban mirror: automatic successor initiation failed closed")
            log.append("automatic_successor: FAILED")
        state = await asyncio.to_thread(load_mirror_state, mirror_conn)
    digest = await asyncio.to_thread(get_digest, mirror_conn)
    note_keys = await asyncio.to_thread(load_note_keys, mirror_conn)
    if (not dry_run and not cfg.reconciliation_enabled
            and await _audit_active_threads(cfg, client, mirror_conn, snapshot, state, log)):
        state = await asyncio.to_thread(load_mirror_state, mirror_conn)
    now = int(time.time())
    ops = plan(snapshot, state, digest, note_keys, cfg, now)

    for op in ops:
        initiative = state.get(str(op.data.get("initiative_id") or ""))
        if (cfg.reconciliation_enabled and initiative is not None
                and _is_quarantined(mirror_conn, initiative.thread_id)):
            log.append(f"{op.kind}: BLOCKED quarantined thread={initiative.thread_id}")
            continue
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
                if cfg.terminal_lifecycle_enabled:
                    log.append(f"archive_thread: DEFERRED {op.data['initiative_id']} (terminal lifecycle)")
                else:
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

    if cfg.terminal_lifecycle_enabled and not dry_run and client is not None:
        # Run after ordinary operations so transition, note, outbox, and starter
        # work gets first chance to drain; lifecycle itself rechecks durable guards.
        state = await asyncio.to_thread(load_mirror_state, mirror_conn)
        await _resume_terminal_lifecycles(cfg, client, mirror_conn, snapshot, state, log)

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


async def run_mirror_daemon(
    is_running: Callable[[], bool], *, allow_process_token_fallback: bool = True
) -> None:
    from plugins.platforms.discord.kanban_mirror.config import load_mirror_config

    cfg = load_mirror_config()
    if not cfg.valid():
        logger.info("kanban mirror: disabled or unconfigured")
        return
    token = load_discord_token(
        cfg.token_env_path,
        allow_process_fallback=allow_process_token_fallback,
    )
    if not token:
        logger.warning("kanban mirror: no DISCORD_BOT_TOKEN at %s; disabled", cfg.token_env_path)
        return
    client = DiscordClient(token)
    conn = connect_mirror(mirror_db_path(cfg.board))
    if cfg.reconciliation_enabled:
        try:
            snapshot = await asyncio.to_thread(load_board_snapshot, cfg.board)
            await _observe_and_reconcile(cfg, client, conn, snapshot, [])
        except Exception:
            logger.exception("kanban mirror: startup live reconciliation failed closed")
    else:
        await reconcile(cfg, client, conn)
    while is_running():
        try:
            await tick(cfg, client, conn)
        except Exception:
            logger.exception("kanban mirror: tick failed")
        await asyncio.sleep(cfg.poll_seconds)
