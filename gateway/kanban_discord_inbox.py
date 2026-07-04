"""Discord Forum reply inbox for Kanban mirror threads.

Phase 1 only: consume authorized replies in configured Discord Forum threads,
write durable Kanban comments or small non-destructive commands, and keep the
normal chat dispatcher out of those messages.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from hermes_cli import kanban_db as kb

from gateway.kanban_mirror.state import (
    connect_mirror,
    ensure_receipts,
    find_receipt_comment_id,
    mirror_db_path,
    receipt_exists,
    record_receipt,
    resolve_thread_task,
)

logger = logging.getLogger(__name__)

_SUPPORTED_ACTIONS = {"comment", "block", "unblock"}
_RESERVED_COMMANDS = {"priority", "assign", "create-child", "create_child", "complete", "delete", "archive"}
_COMMAND_USAGE = "Usage: comment <text>, block <reason>, or unblock"


@dataclass(frozen=True)
class KanbanReplyInboxConfig:
    enabled: bool = False
    forum_channel_ids: frozenset[str] = frozenset()
    allow_commands: frozenset[str] = frozenset({"comment", "block", "unblock"})
    default_action: str = "comment"
    ack: bool = True
    board_slug: str | None = None
    allow_thread_level_messages: bool = False


@dataclass(frozen=True)
class ParsedKanbanInstruction:
    action: Literal["comment", "block", "unblock"]
    text: str = ""


@dataclass(frozen=True)
class DiscordReplyContext:
    message_id: str
    author_id: str | None
    author_label: str
    forum_channel_id: str
    thread_id: str
    content: str
    reply_to_message_id: str | None = None
    reply_to_text: str | None = None


@dataclass(frozen=True)
class ParsedKanbanReaction:
    emoji: str
    intent: str
    meaning: str


@dataclass(frozen=True)
class DiscordReactionContext:
    reaction_key: str
    message_id: str
    author_id: str | None
    author_label: str
    thread_id: str
    emoji: str
    intent: str
    meaning: str


@dataclass(frozen=True)
class KanbanReplyInboxResult:
    consumed: bool
    reason: str
    task_id: str | None = None
    action: str | None = None
    kanban_comment_id: int | None = None
    ack: str | None = None


_REACTION_INTENTS: dict[str, ParsedKanbanReaction] = {
    "✅": ParsedKanbanReaction("✅", "approve", "Approve / done reviewing / LGTM."),
    "⏸": ParsedKanbanReaction("⏸️", "pause", "Pause work; blocked on human input."),
    "🗑": ParsedKanbanReaction("🗑️", "close_request", "Close card or dismiss as noise."),
    "👀": ParsedKanbanReaction("👀", "watch", "Watching; keep me updated."),
    "🔁": ParsedKanbanReaction("🔁", "rerun", "Rerun / try again / rework needed."),
    "🚫": ParsedKanbanReaction("🚫", "reject", "Reject / do not do this."),
    "❔": ParsedKanbanReaction("❔", "needs_context", "Need more context / explanation."),
    "🧐": ParsedKanbanReaction("🧐", "review", "Review closely / question assumptions."),
    "🤔": ParsedKanbanReaction("🤔", "expand", "Interesting — flesh this out."),
}


def _normalize_emoji(emoji: str) -> str:
    return (emoji or "").replace("️", "").strip()


def reaction_intent_for_emoji(emoji: str) -> ParsedKanbanReaction | None:
    return _REACTION_INTENTS.get(_normalize_emoji(emoji))


def _reaction_author_label(payload: Any) -> str:
    member = getattr(payload, "member", None)
    if member is not None:
        label = (
            str(getattr(member, "display_name", "") or "").strip()
            or str(getattr(member, "nick", "") or "").strip()
            or str(getattr(member, "global_name", "") or "").strip()
            or str(getattr(member, "name", "") or "").strip()
        )
        if label:
            return label
    user_id = str(getattr(payload, "user_id", "") or "").strip()
    return user_id or "unknown Discord user"


def context_from_discord_reaction(payload: Any) -> DiscordReactionContext | None:
    thread_id = str(getattr(payload, "channel_id", "") or "").strip()
    message_id = str(getattr(payload, "message_id", "") or "").strip()
    emoji_raw = str(getattr(getattr(payload, "emoji", None), "name", "") or "").strip()
    reaction = reaction_intent_for_emoji(emoji_raw)
    if not thread_id or not message_id or reaction is None:
        return None
    author_id = str(getattr(payload, "user_id", "") or "").strip() or None
    reaction_key = f"reaction:{thread_id}:{message_id}:{author_id or 'unknown'}:{_normalize_emoji(emoji_raw)}"
    return DiscordReactionContext(
        reaction_key=reaction_key,
        message_id=message_id,
        author_id=author_id,
        author_label=_reaction_author_label(payload),
        thread_id=thread_id,
        emoji=reaction.emoji,
        intent=reaction.intent,
        meaning=reaction.meaning,
    )


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_id_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, (list, tuple, set)):
        return frozenset(str(v).strip() for v in value if str(v).strip())
    return frozenset(part.strip() for part in str(value).split(",") if part.strip())


def load_config(raw_config: dict[str, Any] | None = None) -> KanbanReplyInboxConfig:
    """Load ``discord.kanban_reply_inbox`` from config.yaml-shaped data."""
    if raw_config is None:
        try:
            from hermes_cli.config import read_raw_config

            raw_config = read_raw_config() or {}
        except Exception:
            logger.debug("failed to read raw config for Discord Kanban inbox", exc_info=True)
            raw_config = {}
    discord_cfg = raw_config.get("discord") if isinstance(raw_config, dict) else {}
    inbox_cfg = discord_cfg.get("kanban_reply_inbox") if isinstance(discord_cfg, dict) else {}
    if not isinstance(inbox_cfg, dict):
        inbox_cfg = {}
    allow_commands = _as_id_set(inbox_cfg.get("allow_commands")) or frozenset({"comment", "block", "unblock"})
    return KanbanReplyInboxConfig(
        enabled=_as_bool(inbox_cfg.get("enabled"), False),
        forum_channel_ids=_as_id_set(inbox_cfg.get("forum_channel_ids")),
        allow_commands=frozenset(cmd for cmd in allow_commands if cmd in _SUPPORTED_ACTIONS),
        default_action=str(inbox_cfg.get("default_action") or "comment").strip().lower(),
        ack=_as_bool(inbox_cfg.get("ack"), True),
        board_slug=(str(inbox_cfg.get("board_slug")).strip() or None) if inbox_cfg.get("board_slug") is not None else None,
        allow_thread_level_messages=_as_bool(inbox_cfg.get("allow_thread_level_messages"), False),
    )


def parse_instruction(text: str, *, config: KanbanReplyInboxConfig | None = None) -> ParsedKanbanInstruction:
    """Parse a Phase-1 Kanban inbox command.

    No command prefix means a normal durable Kanban comment.
    """
    cfg = config or KanbanReplyInboxConfig()
    body = (text or "").strip()
    if not body:
        raise ValueError("message text is required")
    first, sep, rest = body.partition(" ")
    command = first.rstrip(":").lower()
    if command in _SUPPORTED_ACTIONS:
        if command not in cfg.allow_commands:
            raise ValueError(f"command not allowed: {command}")
        arg = rest.strip() if sep else ""
        if command in {"comment", "block"} and not arg:
            raise ValueError(_COMMAND_USAGE)
        if command == "unblock" and arg:
            raise ValueError("Usage: unblock")
        return ParsedKanbanInstruction(action=command, text=arg)
    if command in _RESERVED_COMMANDS:
        raise ValueError(f"unsupported Kanban inbox command: {command}")
    if cfg.default_action != "comment" or "comment" not in cfg.allow_commands:
        raise ValueError(_COMMAND_USAGE)
    return ParsedKanbanInstruction(action="comment", text=body)


def _find_replied_to_comment_id(
    replied_to_message_id: str | None,
    *,
    mirror_conn: sqlite3.Connection,
) -> int | None:
    if not replied_to_message_id:
        return None
    comment_id = find_receipt_comment_id(mirror_conn, replied_to_message_id)
    if comment_id is None:
        return None
    try:
        return int(comment_id)
    except (TypeError, ValueError):
        return None


def _comment_body(ctx: DiscordReplyContext, parsed: ParsedKanbanInstruction) -> str:
    lines = [
        "[discord instruction]",
        parsed.text.strip(),
        "",
        f"source: discord thread {ctx.thread_id}, message {ctx.message_id}"
        + (f", replied_to {ctx.reply_to_message_id}" if ctx.reply_to_message_id else ""),
        f"author: discord:{ctx.author_label}",
    ]
    if ctx.reply_to_text:
        snippet = re.sub(r"\s+", " ", ctx.reply_to_text).strip()
        if snippet:
            lines.append(f"reply_context: {snippet[:200]}")
    return "\n".join(line for line in lines if line is not None).strip()


def apply_instruction(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    board_slug: str,
    ctx: DiscordReplyContext,
    parsed: ParsedKanbanInstruction,
    mirror_conn: sqlite3.Connection,
) -> KanbanReplyInboxResult:
    """Apply a parsed reply-inbox instruction against a v2 mirror mapping.

    ``conn`` (kanban.db) is used ONLY for ``kb.add_comment`` / ``kb.block_task`` /
    ``kb.unblock_task``. Receipts are recorded in v2 ``mirror.db`` only.
    """
    task_id = str(task_id)
    board_slug = str(board_slug or "") or "default"
    ensure_receipts(mirror_conn)

    def _duplicate() -> bool:
        return receipt_exists(mirror_conn, ctx.message_id)

    if _duplicate():
        return KanbanReplyInboxResult(consumed=True, reason="duplicate", task_id=task_id, action="duplicate")

    replied_to_comment_id = _find_replied_to_comment_id(ctx.reply_to_message_id, mirror_conn=mirror_conn)
    comment_id: int | None = None

    if parsed.action == "comment":
        comment_id = kb.add_comment(conn, task_id, author=f"discord:{ctx.author_label}", body=_comment_body(ctx, parsed))
        ack = f"Recorded on Kanban card {task_id} as comment #{comment_id}."
    elif parsed.action == "block":
        ok = kb.block_task(conn, task_id, reason=parsed.text)
        if not ok:
            raise ValueError(f"cannot block Kanban card {task_id} from its current status")
        comment_id = kb.add_comment(conn, task_id, author=f"discord:{ctx.author_label}", body=_comment_body(ctx, parsed))
        ack = f"Blocked Kanban card {task_id}: {parsed.text}"
    elif parsed.action == "unblock":
        ok = kb.unblock_task(conn, task_id)
        if not ok:
            raise ValueError(f"cannot unblock Kanban card {task_id} from its current status")
        comment_id = kb.add_comment(conn, task_id, author=f"discord:{ctx.author_label}", body=_comment_body(ctx, ParsedKanbanInstruction("comment", "Unblock requested from Discord.")))
        ack = f"Unblocked Kanban card {task_id}."
    else:  # pragma: no cover - parse_instruction prevents this
        raise ValueError(_COMMAND_USAGE)

    # NOTE (accepted risk, per spec): if the process crashes between the
    # kb.add_comment above and the receipt write below, a retry of the same
    # Discord message can duplicate the Kanban comment. Decoupling the
    # receipt store (mirror.db) from kanban.db wins over exactly-once here.
    receipt_kwargs = dict(
        discord_message_id=ctx.message_id,
        board_slug=board_slug,
        forum_channel_id=ctx.forum_channel_id,
        thread_id=ctx.thread_id,
        task_id=task_id,
        author_id=ctx.author_id,
        action=parsed.action,
        replied_to_message_id=ctx.reply_to_message_id,
        replied_to_kanban_comment_id=replied_to_comment_id,
        kanban_comment_id=comment_id,
    )
    if _duplicate():
        return KanbanReplyInboxResult(consumed=True, reason="duplicate", task_id=task_id, action="duplicate")
    record_receipt(mirror_conn, **receipt_kwargs)

    return KanbanReplyInboxResult(
        consumed=True,
        reason="handled",
        task_id=task_id,
        action=parsed.action,
        kanban_comment_id=comment_id,
        ack=ack,
    )


def _reaction_comment_body(ctx: DiscordReactionContext, replied_to_comment_id: int | None) -> str:
    lines = [
        "[discord reaction intent]",
        f"Reaction from discord:{ctx.author_label}",
        f"Emoji: {ctx.emoji}",
        f"Intent: {ctx.intent}",
        f"Meaning: {ctx.meaning}",
        f"Thread: {ctx.thread_id}",
        f"Message: {ctx.message_id}",
        "State change: none (owner-only)",
    ]
    if replied_to_comment_id is not None:
        lines.append(f"Reacted Kanban comment: #{replied_to_comment_id}")
    return "\n".join(lines).strip()


def handle_reaction(
    ctx: DiscordReactionContext,
    *,
    config: KanbanReplyInboxConfig | None = None,
) -> KanbanReplyInboxResult:
    cfg = config or load_config()
    if not cfg.enabled:
        return KanbanReplyInboxResult(consumed=False, reason="disabled")

    board_slug = cfg.board_slug or "default"
    resolved = resolve_thread_task(mirror_db_path(board_slug), forum_channel_id="", thread_id=ctx.thread_id)
    if resolved is None:
        return KanbanReplyInboxResult(consumed=False, reason="unmapped_thread")

    task_id, resolved_board_slug = resolved
    task_id = str(task_id)
    resolved_board_slug = str(resolved_board_slug)
    conn = kb.connect(board=resolved_board_slug)
    mirror_conn = connect_mirror(mirror_db_path(resolved_board_slug))
    try:
        ensure_receipts(mirror_conn)
        if receipt_exists(mirror_conn, ctx.reaction_key):
            return KanbanReplyInboxResult(consumed=True, reason="duplicate", task_id=task_id, action="duplicate")

        replied_to_comment_id = find_receipt_comment_id(mirror_conn, ctx.message_id)
        comment_body = _reaction_comment_body(ctx, replied_to_comment_id)
        comment_id = kb.add_comment(conn, task_id, author=f"discord:{ctx.author_label}", body=comment_body)
        record_receipt(
            mirror_conn,
            discord_message_id=ctx.reaction_key,
            board_slug=resolved_board_slug,
            forum_channel_id="",
            thread_id=ctx.thread_id,
            task_id=task_id,
            author_id=ctx.author_id,
            action=f"reaction:{ctx.intent}",
            replied_to_message_id=ctx.message_id,
            replied_to_kanban_comment_id=replied_to_comment_id,
            kanban_comment_id=comment_id,
        )
        return KanbanReplyInboxResult(
            consumed=True,
            reason="handled",
            task_id=task_id,
            action=f"reaction:{ctx.intent}",
            kanban_comment_id=comment_id,
            ack=f"Recorded reaction on Kanban card {task_id} as comment #{comment_id}.",
        )
    finally:
        mirror_conn.close()
        conn.close()


def handle_reply(
    ctx: DiscordReplyContext,
    *,
    config: KanbanReplyInboxConfig | None = None,
) -> KanbanReplyInboxResult:
    cfg = config or load_config()
    if not cfg.enabled:
        return KanbanReplyInboxResult(consumed=False, reason="disabled")
    if not cfg.forum_channel_ids or ctx.forum_channel_id not in cfg.forum_channel_ids:
        return KanbanReplyInboxResult(consumed=False, reason="forum_not_configured")
    if not ctx.reply_to_message_id and not cfg.allow_thread_level_messages:
        return KanbanReplyInboxResult(consumed=False, reason="not_a_reply")

    board_slug = cfg.board_slug or "default"
    resolved = resolve_thread_task(
        mirror_db_path(board_slug), forum_channel_id=ctx.forum_channel_id, thread_id=ctx.thread_id
    )

    if resolved is None:
        return KanbanReplyInboxResult(consumed=False, reason="unmapped_thread")

    task_id, resolved_board_slug = resolved
    resolved_board_slug = str(resolved_board_slug)
    conn = kb.connect(board=resolved_board_slug)
    try:
        mirror_conn = connect_mirror(mirror_db_path(resolved_board_slug))
        try:
            parsed = parse_instruction(ctx.content, config=cfg)
            return apply_instruction(
                conn,
                task_id=str(task_id),
                board_slug=resolved_board_slug,
                ctx=ctx,
                parsed=parsed,
                mirror_conn=mirror_conn,
            )
        finally:
            mirror_conn.close()
    finally:
        conn.close()


def context_from_discord_message(message: Any) -> DiscordReplyContext | None:
    channel = getattr(message, "channel", None)
    thread_id = str(getattr(channel, "id", "") or "")
    forum_channel_id = str(getattr(channel, "parent_id", "") or "")
    if not thread_id or not forum_channel_id:
        return None
    reference = getattr(message, "reference", None)
    reply_to_message_id = None
    reply_to_text = None
    if reference is not None:
        raw_mid = getattr(reference, "message_id", None)
        reply_to_message_id = str(raw_mid) if raw_mid is not None else None
        resolved = getattr(reference, "resolved", None)
        reply_to_text = getattr(resolved, "content", None) if resolved is not None else None
    author = getattr(message, "author", None)
    author_id = str(getattr(author, "id", "") or "") or None
    author_label = (
        str(getattr(author, "display_name", "") or "").strip()
        or str(getattr(author, "name", "") or "").strip()
        or author_id
        or "unknown"
    )
    message_id = str(getattr(message, "id", "") or "")
    content = str(getattr(message, "content", "") or "")
    if not message_id:
        return None
    return DiscordReplyContext(
        message_id=message_id,
        author_id=author_id,
        author_label=author_label,
        forum_channel_id=forum_channel_id,
        thread_id=thread_id,
        content=content,
        reply_to_message_id=reply_to_message_id,
        reply_to_text=reply_to_text,
    )


async def maybe_handle_discord_message(
    message: Any,
    *,
    config: KanbanReplyInboxConfig | None = None,
    mark_nonconversational=None,
) -> KanbanReplyInboxResult:
    ctx = context_from_discord_message(message)
    if ctx is None:
        return KanbanReplyInboxResult(consumed=False, reason="not_thread_message")
    cfg = config or load_config()
    try:
        result = handle_reply(ctx, config=cfg)
    except ValueError as exc:
        result = KanbanReplyInboxResult(consumed=True, reason="rejected", ack=f"Rejected: {exc}")
    if result.consumed and cfg.ack and result.ack:
        try:
            sent = await message.reply(result.ack, mention_author=False)
            sent_id = str(getattr(sent, "id", "") or "")
            if sent_id and mark_nonconversational is not None:
                mark_nonconversational([sent_id])
        except Exception:
            logger.warning("failed to acknowledge Discord Kanban inbox message", exc_info=True)
    return result


async def maybe_handle_discord_reaction(
    payload: Any,
    *,
    config: KanbanReplyInboxConfig | None = None,
) -> KanbanReplyInboxResult:
    ctx = context_from_discord_reaction(payload)
    if ctx is None:
        return KanbanReplyInboxResult(consumed=False, reason="unsupported_reaction")
    cfg = config or load_config()
    try:
        return handle_reaction(ctx, config=cfg)
    except ValueError as exc:
        return KanbanReplyInboxResult(consumed=True, reason="rejected", ack=f"Rejected: {exc}")
