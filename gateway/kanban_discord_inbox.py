"""Discord Forum reply inbox for Kanban mirror threads.

Phase 1 only: consume authorized replies in configured Discord Forum threads,
write durable Kanban comments or small non-destructive commands, and keep the
normal chat dispatcher out of those messages.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

from hermes_cli import kanban_db as kb

from gateway.kanban_mirror.conversation_log import (
    freeze_log_delivery,
    mark_log_delivery,
    parse_log_command,
    record_conversation_event,
    resolve_log_targets,
)
from gateway.kanban_mirror.state import (
    connect_mirror,
    ensure_receipts,
    find_receipt_comment_id,
    mark_reaction_active,
    mark_reaction_removed,
    mirror_db_path,
    reaction_generation,
    receipt_exists,
    record_receipt,
    resolve_thread_task,
)

logger = logging.getLogger(__name__)

_UNRESOLVED_CHANNEL = object()
_SUPPORTED_ACTIONS = {"comment", "block", "unblock"}
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
    # Independent of legacy reply ingestion and deliberately off by default.
    conversation_log_enabled: bool = False
    conversation_router_enabled: bool = False
    conversation_router_ingress_bot_id: str | None = None
    profile_bot_user_ids: tuple[tuple[str, str], ...] = ()


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
    mentioned_user_ids: tuple[str, ...] = ()
    replied_to_author_id: str | None = None
    replied_to_author_is_bot: bool = False
    discord_created_at: int | None = None
    message_link: str | None = None
    attachments: tuple[dict, ...] = ()


@dataclass(frozen=True)
class ProfileRoute:
    profile: str | None
    basis: Literal["explicit_mention", "reply_to_profile_bot", "card_owner", "none"]
    mentioned_profiles: tuple[str, ...] = ()
    profiles: tuple[str, ...] = ()
    error: str | None = None


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
    forum_channel_id: str
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
    routed_task_id: str | None = None
    owner_instruction_id: int | None = None
    owner_instruction_status: str | None = None
    ack: str | None = None
    route_profile: str | None = None
    route_profiles: tuple[str, ...] = ()
    correlation_id: str | None = None
    card_context: str | None = None
    ingress_bot_id: str | None = None


_REACTION_INTENTS: dict[str, ParsedKanbanReaction] = {
    "✅": ParsedKanbanReaction("✅", "approve", "Approve / done reviewing / LGTM."),
    "⏸": ParsedKanbanReaction("⏸️", "pause", "Pause work; blocked on human input."),
    "🗑": ParsedKanbanReaction("🗑️", "close_request", "Close card or dismiss as noise."),
    "👀": ParsedKanbanReaction("👀", "watch", "Watching; keep me updated."),
    "🔁": ParsedKanbanReaction("🔁", "rerun_request", "Rerun / try again / rework needed."),
    "🚫": ParsedKanbanReaction("🚫", "reject", "Reject / do not do this."),
    "❔": ParsedKanbanReaction("❔", "needs_context", "Need more context / explanation."),
    "🧐": ParsedKanbanReaction("🧐", "review_request", "Review closely / question assumptions."),
    "🤔": ParsedKanbanReaction("🤔", "expand_idea", "Interesting — flesh this out."),
}


_TEXT_ACTION_ALIASES: dict[str, str] = {
    "approve": "✅", "approved": "✅", "yes": "✅",
    "pause": "⏸", "stop": "⏸",
    "close": "🗑",
    "watch": "👀",
    "rerun": "🔁", "redo": "🔁",
    "reject": "🚫", "rejected": "🚫", "no": "🚫",
    "context": "❔",
    "review": "🧐",
    "expand": "🤔",
}


_DIRECTIVE_EMOJIS: dict[str, str] = {
    "approve": "✅",
    "pause": "⏸",
    "close": "🗑",
    "watch": "👀",
    "rerun": "🔁",
    "reject": "🚫",
    "context": "❔",
    "review": "🧐",
    "expand": "🤔",
}


def directive_for_text(text: str) -> ParsedKanbanReaction | None:
    """Resolve a canonical ``!directive`` token; unknown commands are conversation."""
    body = (text or "").strip()
    if not body.startswith("!"):
        return None
    token = body.split(None, 1)[0][1:].casefold()
    emoji = _DIRECTIVE_EMOJIS.get(token)
    return _REACTION_INTENTS.get(emoji) if emoji is not None else None


def text_action_for_command(text: str) -> ParsedKanbanReaction | None:
    """Resolve an exact bare action after trimming edge whitespace/punctuation."""
    command = (text or "").casefold()
    start = 0
    end = len(command)
    while start < end and (command[start].isspace() or unicodedata.category(command[start]).startswith("P")):
        start += 1
    while end > start and (command[end - 1].isspace() or unicodedata.category(command[end - 1]).startswith("P")):
        end -= 1
    command = command[start:end]
    emoji = _TEXT_ACTION_ALIASES.get(command)
    return _REACTION_INTENTS.get(emoji) if emoji is not None else None


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


def context_from_discord_reaction(
    payload: Any, *, resolved_channel: Any = _UNRESOLVED_CHANNEL
) -> DiscordReactionContext | None:
    thread_id = str(getattr(payload, "channel_id", "") or "").strip()
    message_id = str(getattr(payload, "message_id", "") or "").strip()
    emoji_raw = str(getattr(getattr(payload, "emoji", None), "name", "") or "").strip()
    reaction = reaction_intent_for_emoji(emoji_raw)
    if not thread_id or not message_id or reaction is None:
        return None
    channel = (
        getattr(payload, "channel", None)
        if resolved_channel is _UNRESOLVED_CHANNEL
        else resolved_channel
    )
    forum_channel_id = str(getattr(channel, "parent_id", "") or "").strip()
    author_id = str(getattr(payload, "user_id", "") or "").strip() or None
    reaction_key = f"reaction:{thread_id}:{message_id}:{author_id or 'unknown'}:{_normalize_emoji(emoji_raw)}"
    return DiscordReactionContext(
        reaction_key=reaction_key,
        message_id=message_id,
        author_id=author_id,
        author_label=_reaction_author_label(payload),
        forum_channel_id=forum_channel_id,
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


def _as_profile_bot_pairs(value: Any) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise ValueError("profile_bot_user_ids must be a mapping")
    from hermes_cli.profiles import normalize_profile_name, validate_profile_name

    pairs: list[tuple[str, str]] = []
    for raw_bot_id, raw_profile in value.items():
        bot_id = str(raw_bot_id or "").strip()
        profile = normalize_profile_name(str(raw_profile or ""))
        if not bot_id.isdigit():
            raise ValueError("profile bot user IDs must be numeric")
        validate_profile_name(profile)
        pairs.append((bot_id, profile))
    return tuple(sorted(pairs))


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
        conversation_log_enabled=_as_bool(inbox_cfg.get("conversation_log_enabled"), False),
        conversation_router_enabled=_as_bool(inbox_cfg.get("conversation_router_enabled"), False),
        conversation_router_ingress_bot_id=(
            str(inbox_cfg.get("conversation_router_ingress_bot_id") or "").strip() or None
        ),
        profile_bot_user_ids=_as_profile_bot_pairs(inbox_cfg.get("profile_bot_user_ids")),
    )


def validate_router_config(cfg: KanbanReplyInboxConfig, *, multiplex_profiles: bool,
                           profile_exists_fn=None, mirror_config=None) -> str | None:
    """Return the ingress profile or raise an actionable, secret-free error."""
    if not (cfg.enabled and cfg.conversation_router_enabled):
        return None
    errors: list[str] = []
    if not multiplex_profiles:
        errors.append("gateway.multiplex_profiles must be enabled")
    if not cfg.conversation_router_ingress_bot_id:
        errors.append("conversation_router_ingress_bot_id is required")
    elif not cfg.conversation_router_ingress_bot_id.isdigit():
        errors.append("conversation_router_ingress_bot_id must be numeric")
    if not cfg.board_slug:
        errors.append("board_slug is required")
    if not cfg.forum_channel_ids:
        errors.append("forum_channel_ids must contain at least one Forum")
    pairs = tuple(cfg.profile_bot_user_ids)
    bot_ids = [bot_id for bot_id, _ in pairs]
    profiles = [profile for _, profile in pairs]
    if not pairs:
        errors.append("profile_bot_user_ids must map every router bot to a profile")
    if len(bot_ids) != len(set(bot_ids)):
        errors.append("profile_bot_user_ids contains duplicate bot IDs")
    if len(profiles) != len(set(profiles)):
        errors.append("profile_bot_user_ids contains duplicate profiles")
    if profile_exists_fn is None:
        from hermes_cli.profiles import profile_exists as profile_exists_fn
    missing = sorted({profile for profile in profiles if not profile_exists_fn(profile)})
    if missing:
        errors.append("mapped profiles do not exist: " + ", ".join(missing))
    ingress_matches = [profile for bot_id, profile in pairs
                       if bot_id == cfg.conversation_router_ingress_bot_id]
    if cfg.conversation_router_ingress_bot_id and not ingress_matches:
        errors.append("ingress bot ID must be present in profile_bot_user_ids")
    if mirror_config is not None and getattr(mirror_config, "enabled", False):
        if str(getattr(mirror_config, "board", "")) != cfg.board_slug:
            errors.append("router board_slug must match kanban.discord_mirror.board")
        forum = str(getattr(mirror_config, "forum_channel_id", ""))
        if forum not in cfg.forum_channel_ids:
            errors.append("router Forums must include kanban.discord_mirror.forum_channel_id")
    if errors:
        raise ValueError("Discord conversation router configuration invalid: " + "; ".join(errors))
    return ingress_matches[0]


def parse_instruction(text: str, *, config: KanbanReplyInboxConfig | None = None) -> ParsedKanbanInstruction:
    """Parse a Phase-1 Kanban inbox command.

    No command prefix means a normal durable Kanban comment.
    """
    cfg = config or KanbanReplyInboxConfig()
    body = (text or "").strip()
    if not body:
        raise ValueError("message text is required")
    parts = body.split(None, 1)
    command = parts[0].rstrip(":").lower()
    if command in _SUPPORTED_ACTIONS:
        if command not in cfg.allow_commands:
            raise ValueError(f"command not allowed: {command}")
        arg = parts[1].strip() if len(parts) > 1 else ""
        if command in {"comment", "block"} and not arg:
            raise ValueError(_COMMAND_USAGE)
        if command == "unblock" and arg:
            raise ValueError("Usage: unblock")
        return ParsedKanbanInstruction(action=command, text=arg)
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


def _reaction_author(ctx: DiscordReactionContext) -> str:
    """Return a safe, stable provenance label for a Discord reaction actor."""
    author_id = str(ctx.author_id or "").strip()
    return f"discord:{author_id}" if author_id.isdigit() else "discord:unknown"


def _reaction_comment_body(
    ctx: DiscordReactionContext,
    replied_to_comment_id: int | None,
    source_key: str,
) -> str:
    lines = [
        "[discord reaction instruction]",
        f"Instruction from {_reaction_author(ctx)}",
        f"Emoji: {ctx.emoji}",
        f"Instruction: {ctx.intent}",
        f"Meaning: {ctx.meaning}",
        f"Thread: {ctx.thread_id}",
        f"Message: {ctx.message_id}",
        f"Reaction key: {source_key}",
        "Lifecycle routing: original card (or passive watch)",
    ]
    if replied_to_comment_id is not None:
        lines.append(f"Reacted Kanban comment: #{replied_to_comment_id}")
    return "\n".join(lines).strip()


def _reaction_followup_body(ctx: DiscordReactionContext, original_task_id: str) -> str:
    """Build a dispatchable owner instruction without changing the source card."""
    return "\n".join([
        "[discord reaction instruction]",
        f"Original card: {original_task_id}",
        f"Instruction from {_reaction_author(ctx)}",
        f"Emoji: {ctx.emoji}",
        f"Instruction: {ctx.intent}",
        f"Meaning: {ctx.meaning}",
        f"Discord thread: {ctx.thread_id}",
        f"Discord message: {ctx.message_id}",
        "",
        "Carry out this instruction or leave a durable explanation on the original card for refusing it.",
    ])


def _find_reaction_comment_id(conn: sqlite3.Connection, task_id: str, reaction_key: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM task_comments WHERE task_id = ? AND body LIKE ? ORDER BY id DESC LIMIT 1",
        (task_id, f"%Reaction key: {reaction_key}%"),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def handle_reaction(
    ctx: DiscordReactionContext,
    *,
    config: KanbanReplyInboxConfig | None = None,
) -> KanbanReplyInboxResult:
    cfg = config or load_config()
    if not cfg.enabled:
        return KanbanReplyInboxResult(consumed=False, reason="disabled")

    board_slug = cfg.board_slug or "default"
    resolved = resolve_thread_task(mirror_db_path(board_slug), forum_channel_id=ctx.forum_channel_id, thread_id=ctx.thread_id)
    if resolved is None:
        return KanbanReplyInboxResult(consumed=False, reason="unmapped_thread")

    task_id, resolved_board_slug = resolved
    task_id = str(task_id)
    resolved_board_slug = str(resolved_board_slug)
    conn = kb.connect(board=resolved_board_slug)
    mirror_conn = connect_mirror(mirror_db_path(resolved_board_slug))
    try:
        ensure_receipts(mirror_conn)
        mirror_conn.execute("BEGIN IMMEDIATE")
        if receipt_exists(mirror_conn, ctx.reaction_key):
            mirror_conn.rollback()
            return KanbanReplyInboxResult(consumed=True, reason="duplicate", task_id=task_id, action="duplicate")

        task = kb.get_task(conn, task_id)
        if task is None:
            return KanbanReplyInboxResult(consumed=False, reason="missing_task", task_id=task_id)
        target_assignee = task.assignee or "unassigned"
        if cfg.conversation_router_enabled:
            directive = ParsedKanbanReaction(ctx.emoji, ctx.intent, ctx.meaning)
            turn_ctx = DiscordReplyContext(
                message_id=ctx.reaction_key, author_id=ctx.author_id,
                author_label=ctx.author_label, forum_channel_id=ctx.forum_channel_id,
                thread_id=ctx.thread_id,
                content=f"{ctx.emoji} {ctx.intent} (reaction to Discord message {ctx.message_id})",
                reply_to_message_id=ctx.message_id,
            )
            event = record_conversation_event(
                mirror_conn, discord_message_id=ctx.reaction_key,
                thread_id=ctx.thread_id, binding_key=None,
                legacy_binding_key=task_id, event_class="directive.user",
                author_label=ctx.author_label, author_id=ctx.author_id,
                content=turn_ctx.content, replied_to_message_id=ctx.message_id,
            )
            route = resolve_profile_route(turn_ctx, owner=str(target_assignee), config=cfg)
            result = _routed_turn_result(
                ctx=turn_ctx, task_id=task_id, board_slug=resolved_board_slug,
                event_id=event.id, route=route,
                ingress_bot_id=cfg.conversation_router_ingress_bot_id,
                directive=directive,
            )
            return KanbanReplyInboxResult(
                **{**result.__dict__, "action": f"reaction:{ctx.intent}"}
            )
        if cfg.conversation_router_enabled:
            try:
                from hermes_cli.profiles import normalize_profile_name, profile_exists
                target_assignee = normalize_profile_name(target_assignee)
                if not profile_exists(target_assignee):
                    raise ValueError("profile does not exist")
            except Exception:
                mirror_conn.rollback()
                return KanbanReplyInboxResult(
                    consumed=True,
                    reason="invalid_profile",
                    task_id=task_id,
                    action=f"reaction:{ctx.intent}",
                    ingress_bot_id=cfg.conversation_router_ingress_bot_id,
                )
        generation = reaction_generation(mirror_conn, ctx.reaction_key)
        source_key = (
            ctx.reaction_key
            if generation == 0
            else f"{ctx.reaction_key}:generation:{generation}"
        )
        reaction_source = (
            "discord_router_reaction" if cfg.conversation_router_enabled else "discord_reaction"
        )
        unresolved = conn.execute(
            """SELECT id,source_key FROM task_owner_instructions
               WHERE task_id=? AND source=?
                 AND status IN ('pending','queued','unroutable')
                 AND (source_key=? OR source_key LIKE ?)
               ORDER BY id DESC LIMIT 1""",
            (task_id, reaction_source, ctx.reaction_key, f"{ctx.reaction_key}:generation:%"),
        ).fetchone()
        if unresolved is not None:
            source_key = str(unresolved["source_key"])
            instruction = kb.get_owner_instruction(conn, int(unresolved["id"]))
        else:
            instruction = kb.create_owner_instruction(
                conn,
                task_id=task_id,
                assignee=target_assignee,
                source=reaction_source,
                source_key=source_key,
                actor=_reaction_author(ctx),
                body=_reaction_followup_body(ctx, task_id),
            )
        if instruction is None:  # pragma: no cover - row disappeared inside one connection
            raise RuntimeError("owner instruction disappeared during reaction handling")
        replied_to_comment_id = find_receipt_comment_id(mirror_conn, ctx.message_id)
        comment_id = _find_reaction_comment_id(conn, task_id, source_key)
        if comment_id is None:
            comment_body = (_reaction_comment_body(ctx, replied_to_comment_id, source_key)
                            + f"\nOwner instruction: #{instruction.id} ({instruction.status})")
            comment_id = kb.add_comment(conn, task_id, author=_reaction_author(ctx), body=comment_body)
        routed_status = instruction.status
        if not cfg.conversation_router_enabled:
            routed_status = kb.route_owner_instruction(
                conn, instruction.id,
                explicit_rerun=ctx.intent == "rerun_request",
                passive=ctx.intent == "watch",
            )
        instruction = kb.get_owner_instruction(conn, instruction.id)
        assert instruction is not None
        mark_reaction_active(mirror_conn, ctx.reaction_key)
        record_receipt(
            mirror_conn,
            discord_message_id=ctx.reaction_key,
            board_slug=resolved_board_slug,
            forum_channel_id=ctx.forum_channel_id,
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
            owner_instruction_id=instruction.id,
            owner_instruction_status=instruction.status,
            ack=f"Recorded owner instruction #{instruction.id} ({routed_status}) on Kanban card {task_id}.",
        )
    finally:
        mirror_conn.close()
        conn.close()


def _reply_author(ctx: DiscordReplyContext) -> str:
    author_id = str(ctx.author_id or "").strip()
    return f"discord:{author_id}" if author_id.isdigit() else "discord:unknown"


def _handle_text_action(
    conn: sqlite3.Connection,
    mirror_conn: sqlite3.Connection,
    *,
    task_id: str,
    board_slug: str,
    ctx: DiscordReplyContext,
    action: ParsedKanbanReaction,
    target_profile: str | None = None,
    action_prefix: str = "text",
) -> KanbanReplyInboxResult:
    source_key = f"{action_prefix}:{ctx.thread_id}:{ctx.message_id}"
    ensure_receipts(mirror_conn)
    mirror_conn.execute("BEGIN IMMEDIATE")
    if receipt_exists(mirror_conn, ctx.message_id):
        mirror_conn.rollback()
        return KanbanReplyInboxResult(consumed=True, reason="duplicate", task_id=task_id, action="duplicate")
    task = kb.get_task(conn, task_id)
    if task is None:
        mirror_conn.rollback()
        return KanbanReplyInboxResult(consumed=False, reason="missing_task", task_id=task_id)
    reply_context = re.sub(r"\s+", " ", ctx.reply_to_text).strip()[:200] if ctx.reply_to_text else None
    body = "\n".join([
        "[discord text instruction]",
        f"Original card: {task_id}",
        f"Instruction from {_reply_author(ctx)}",
        f"Command: {(ctx.content or '').strip()}",
        f"Instruction: {action.intent}",
        f"Meaning: {action.meaning}",
        f"Discord thread: {ctx.thread_id}",
        f"Discord message: {ctx.message_id}",
        *([f"Reply context: {reply_context}"] if reply_context else []),
        "",
        "Carry out this instruction or leave a durable explanation on the original card for refusing it.",
    ])
    instruction = kb.create_owner_instruction(
        conn,
        task_id=task_id,
        assignee=target_profile or task.assignee or "unassigned",
        source="discord_directive" if action_prefix == "directive" else "discord_text_command",
        source_key=source_key,
        actor=_reply_author(ctx),
        body=body,
    )
    marker = f"Text instruction key: {source_key}"
    row = conn.execute(
        "SELECT id FROM task_comments WHERE task_id=? AND body LIKE ? ORDER BY id DESC LIMIT 1",
        (task_id, f"%{marker}%"),
    ).fetchone()
    comment_id = int(row["id"]) if row is not None else kb.add_comment(
        conn,
        task_id,
        author=_reply_author(ctx),
        body="\n".join([
            "[discord text instruction]",
            f"Instruction from {_reply_author(ctx)}",
            f"Command: {(ctx.content or '').strip()}",
            f"Instruction: {action.intent}",
            f"Meaning: {action.meaning}",
            *([f"Reply context: {reply_context}"] if reply_context else []),
            marker,
            f"Owner instruction: #{instruction.id} ({instruction.status})",
            "Lifecycle routing: original card (or passive watch)",
        ]),
    )
    routed_status = instruction.status
    if action_prefix != "directive":
        routed_status = kb.route_owner_instruction(
            conn, instruction.id,
            explicit_rerun=action.intent == "rerun_request",
            passive=action.intent == "watch",
        )
    instruction = kb.get_owner_instruction(conn, instruction.id)
    assert instruction is not None
    record_receipt(
        mirror_conn,
        discord_message_id=ctx.message_id,
        board_slug=board_slug,
        forum_channel_id=ctx.forum_channel_id,
        thread_id=ctx.thread_id,
        task_id=task_id,
        author_id=ctx.author_id,
        action=f"{action_prefix}:{action.intent}",
        replied_to_message_id=ctx.reply_to_message_id,
        replied_to_kanban_comment_id=_find_replied_to_comment_id(ctx.reply_to_message_id, mirror_conn=mirror_conn),
        kanban_comment_id=comment_id,
    )
    if action_prefix == "directive":
        record_conversation_event(
            mirror_conn, discord_message_id=f"{ctx.message_id}:disposition",
            thread_id=ctx.thread_id, binding_key=None, legacy_binding_key=task_id,
            event_class="directive.agent_disposition",
            author_label=target_profile or "agent",
            content=f"{action.intent}: owner instruction #{instruction.id} ({instruction.status})",
            replied_to_message_id=ctx.message_id,
            reply_context=(ctx.content or "").strip(),
        )
    return KanbanReplyInboxResult(
        consumed=True,
        reason="handled",
        task_id=task_id,
        action=f"{action_prefix}:{action.intent}",
        kanban_comment_id=comment_id,
        owner_instruction_id=instruction.id,
        owner_instruction_status=instruction.status,
        ack=f"Recorded owner instruction #{instruction.id} ({routed_status}) on Kanban card {task_id}.",
    )


def _handle_log_command(
    conn: sqlite3.Connection,
    mirror_conn: sqlite3.Connection,
    *,
    task_id: str,
    ctx: DiscordReplyContext,
) -> KanbanReplyInboxResult:
    """Freeze and deliver one explicit conversation export exactly once."""
    command = parse_log_command(ctx.content, replied_to_message_id=ctx.reply_to_message_id)
    if command is None:  # guarded by caller
        raise ValueError("not a log command")
    targets = resolve_log_targets(
        mirror_conn, command=command, thread_id=ctx.thread_id,
        legacy_task_id=task_id,
    )
    deliveries = []
    for target in targets:
        operation_id = "discord-log:" + hashlib.sha256(
            f"{ctx.message_id}\0{target.task_id}\0{target.binding_key or ''}".encode("utf-8")
        ).hexdigest()
        delivery = freeze_log_delivery(
            mirror_conn, operation_id=operation_id,
            trigger_discord_message_id=ctx.message_id, thread_id=ctx.thread_id,
            task_id=target.task_id, command=command,
            binding_key=target.binding_key,
            scope_all_to_binding=(command.mode == "all" and target.binding_key is not None),
        )
        if delivery is not None:
            deliveries.append((target, delivery, operation_id))
    if not deliveries:
        return KanbanReplyInboxResult(
            consumed=True, reason="nothing_to_log", task_id=task_id, action="log",
            ack=f"No unsent conversation found for Kanban card {task_id}.",
        )
    if all(delivery.status == "delivered" for _target, delivery, _op in deliveries):
        return KanbanReplyInboxResult(
            consumed=True, reason="duplicate", task_id=task_id, action="log",
            kanban_comment_id=deliveries[-1][1].kanban_comment_id,
        )

    # Each epoch has its own frozen payload and destination. Mark it delivered
    # only after Kanban confirms the idempotently marked comment.
    comment_id = None
    for target, delivery, operation_id in deliveries:
        if delivery.status == "delivered":
            comment_id = delivery.kanban_comment_id
            continue
        chunks = mirror_conn.execute(
            """SELECT * FROM mirror_conversation_delivery_chunks
               WHERE operation_id=? ORDER BY chunk_index""", (operation_id,),
        ).fetchall()
        try:
            for chunk in chunks:
                if chunk["status"] == "delivered":
                    comment_id = int(chunk["kanban_comment_id"])
                    continue
                marker = (
                    f"[discord-log-operation:{operation_id}:"
                    f"{chunk['chunk_index'] + 1}/{chunk['chunk_count']}]"
                )
                comment_id, _created = kb.add_comment_once(
                    conn, target.task_id, author=_reply_author(ctx),
                    body=f"{chunk['payload']}\n\n{marker}", idempotency_marker=marker,
                )
                mirror_conn.execute(
                    """UPDATE mirror_conversation_delivery_chunks
                       SET status='delivered',attempt_count=attempt_count+1,
                           kanban_comment_id=?,delivered_at=?,last_error=NULL
                       WHERE operation_id=? AND chunk_index=?""",
                    (comment_id, int(time.time()), operation_id, chunk["chunk_index"]),
                )
                mirror_conn.commit()
        except Exception as exc:
            mirror_conn.execute(
                """UPDATE mirror_conversation_delivery_chunks
                   SET status='failed',attempt_count=attempt_count+1,last_error=?,next_attempt_at=?
                   WHERE operation_id=? AND status!='delivered'""",
                (str(exc), int(time.time()) + 2, operation_id),
            )
            mirror_conn.commit()
            mark_log_delivery(
                mirror_conn, operation_id=operation_id, status="failed", error=str(exc)
            )
            raise
        # Parent delivery (and therefore its source events) becomes delivered
        # only after every frozen chunk has a confirmed comment id.
        mark_log_delivery(
            mirror_conn, operation_id=operation_id, status="delivered",
            kanban_comment_id=comment_id,
        )
    assert comment_id is not None
    return KanbanReplyInboxResult(
        consumed=True, reason="handled", task_id=task_id, action="log",
        kanban_comment_id=comment_id,
        ack=f"Logged Discord conversation on Kanban as {len(deliveries)} binding-scoped comment(s).",
    )


def resolve_profile_route(
    ctx: DiscordReplyContext,
    *,
    owner: str | None,
    config: KanbanReplyInboxConfig,
) -> ProfileRoute:
    """Resolve explicit mentions, a replied bot, or the card owner to profiles."""
    from hermes_cli.profiles import normalize_profile_name, profile_exists

    bot_profiles = dict(config.profile_bot_user_ids)
    mentioned = tuple(
        dict.fromkeys(
            bot_profiles[user_id]
            for user_id in ctx.mentioned_user_ids
            if user_id in bot_profiles
        )
    )
    if mentioned:
        candidates, basis = mentioned, "explicit_mention"
    elif (
        ctx.replied_to_author_is_bot
        and ctx.replied_to_author_id
        and ctx.replied_to_author_id in bot_profiles
    ):
        candidates, basis = (bot_profiles[ctx.replied_to_author_id],), "reply_to_profile_bot"
    else:
        candidates = (normalize_profile_name(owner),) if owner else ()
        basis = "card_owner"
    profiles = tuple(normalize_profile_name(candidate) for candidate in candidates)
    if not profiles or any(not profile_exists(candidate) for candidate in profiles):
        return ProfileRoute(profile=None, basis="none", mentioned_profiles=mentioned, error="invalid_profile")
    return ProfileRoute(
        profile=profiles[0],
        basis=basis,
        mentioned_profiles=mentioned,
        profiles=profiles,
    )


def _event_bound_task_id(
    mirror_conn: sqlite3.Connection, *, thread_id: str, binding_key: str | None,
    legacy_task_id: str,
) -> str | None:
    """Resolve the card captured with an event, retaining epoch-less mirrors."""
    if binding_key is not None:
        row = mirror_conn.execute(
            "SELECT task_id FROM mirror_binding_epochs WHERE thread_id=? AND binding_key=?",
            (str(thread_id), str(binding_key)),
        ).fetchone()
        if row is not None:
            return str(row["task_id"])
    epoch_count = mirror_conn.execute(
        "SELECT COUNT(*) FROM mirror_binding_epochs WHERE thread_id=?", (str(thread_id),)
    ).fetchone()[0]
    return str(legacy_task_id) if not epoch_count else None


def _routed_turn_result(*, ctx: DiscordReplyContext, task_id: str, board_slug: str,
                        event_id: int, route: ProfileRoute,
                        ingress_bot_id: str | None,
                        directive: ParsedKanbanReaction | None = None) -> KanbanReplyInboxResult:
    """Describe an agent turn without mutating the Kanban card."""
    action = f"directive:{directive.intent}" if directive else "conversation"
    if route.profile is None or not ingress_bot_id:
        return KanbanReplyInboxResult(
            consumed=True, reason=route.error or "ambiguous_owner", task_id=task_id,
            action=action, owner_instruction_id=event_id, ingress_bot_id=ingress_bot_id,
        )
    correlation = "discord:" + hashlib.sha256(
        f"{ctx.thread_id}\0{ctx.message_id}".encode("utf-8")
    ).hexdigest()
    extra = ""
    if directive:
        owner_only = directive.intent in {"approve", "pause", "close_request", "rerun_request", "reject"}
        extra = (f" This is a Discord Kanban directive ({directive.intent}: {directive.meaning})."
                 " You may accept, refuse, or ask for clarification."
                 + (" Card mutation is owner-authorized only; advisory targets must not mutate it."
                    if owner_only else ""))
    return KanbanReplyInboxResult(
        consumed=False, reason="conversation_routed", task_id=task_id, action=action,
        owner_instruction_id=event_id, route_profile=route.profile,
        route_profiles=route.profiles, correlation_id=correlation,
        card_context=(f"Kanban card {task_id} (board {board_slug}, target profiles "
                      f"{', '.join(route.profiles)}, route basis {route.basis}).{extra}"),
        ingress_bot_id=ingress_bot_id,
    )


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
    log_command = parse_log_command(ctx.content, replied_to_message_id=ctx.reply_to_message_id)
    log_enabled = cfg.conversation_log_enabled and log_command is not None
    if (
        not ctx.reply_to_message_id
        and not cfg.allow_thread_level_messages
        and not log_enabled
        and not cfg.conversation_router_enabled
    ):
        return KanbanReplyInboxResult(consumed=False, reason="not_a_reply")

    board_slug = cfg.board_slug or "default"
    mirror_path = mirror_db_path(board_slug)
    resolved = resolve_thread_task(
        mirror_path, forum_channel_id=ctx.forum_channel_id, thread_id=ctx.thread_id
    )

    if resolved is None:
        # A known epoch-backed thread can be intentionally unresolved when it
        # has zero/ambiguous active epochs or is quarantined. Preserve routed
        # human input with a NULL binding, but do not route it to any card.
        if cfg.conversation_router_enabled and mirror_path.exists():
            mirror_conn = connect_mirror(mirror_path)
            try:
                epoch_count = mirror_conn.execute(
                    "SELECT COUNT(*) FROM mirror_binding_epochs WHERE thread_id=?",
                    (ctx.thread_id,),
                ).fetchone()[0]
                directive = directive_for_text(ctx.content)
                words = (ctx.content or "").strip().split(None, 1)
                first_word = words[0].rstrip(":").lower() if words else ""
                explicit = log_command is not None or first_word in _SUPPORTED_ACTIONS
                if epoch_count and (directive is not None or not explicit):
                    event = record_conversation_event(
                        mirror_conn, discord_message_id=ctx.message_id,
                        thread_id=ctx.thread_id, binding_key=None,
                        event_class=("directive.user" if directive else "conversation.human"),
                        author_label=ctx.author_label, content=ctx.content,
                        replied_to_message_id=ctx.reply_to_message_id,
                        author_id=ctx.author_id, discord_created_at=ctx.discord_created_at,
                        discord_message_link=ctx.message_link, reply_context=ctx.reply_to_text,
                        attachments=ctx.attachments,
                    )
                    return KanbanReplyInboxResult(
                        consumed=True, reason="binding_unavailable",
                        action=(f"directive:{directive.intent}" if directive else "conversation"),
                        owner_instruction_id=event.id,
                        ingress_bot_id=cfg.conversation_router_ingress_bot_id,
                    )
            finally:
                mirror_conn.close()
        return KanbanReplyInboxResult(consumed=False, reason="unmapped_thread")

    task_id, resolved_board_slug = resolved
    resolved_board_slug = str(resolved_board_slug)
    conn = kb.connect(board=resolved_board_slug)
    try:
        mirror_conn = connect_mirror(mirror_db_path(resolved_board_slug))
        try:
            directive = directive_for_text(ctx.content) if cfg.conversation_router_enabled else None
            text_action = (
                directive
                if directive is not None
                else (None if cfg.conversation_router_enabled else text_action_for_command(ctx.content))
            )
            words = (ctx.content or "").strip().split(None, 1)
            first_word = words[0].rstrip(":").lower() if words else ""
            explicit = log_command is not None or text_action is not None or first_word in _SUPPORTED_ACTIONS
            if cfg.conversation_router_enabled and directive is not None:
                event = record_conversation_event(
                    mirror_conn, discord_message_id=ctx.message_id,
                    thread_id=ctx.thread_id, binding_key=None,
                    legacy_binding_key=str(task_id),
                    event_class="directive.user", author_label=ctx.author_label,
                    content=ctx.content, replied_to_message_id=ctx.reply_to_message_id,
                    author_id=ctx.author_id, discord_created_at=ctx.discord_created_at,
                    discord_message_link=ctx.message_link, reply_context=ctx.reply_to_text,
                    attachments=ctx.attachments,
                )
                event_task_id = _event_bound_task_id(
                    mirror_conn, thread_id=ctx.thread_id,
                    binding_key=event.binding_key, legacy_task_id=str(task_id),
                )
                if event_task_id is None:
                    return KanbanReplyInboxResult(
                        consumed=True, reason="binding_unavailable",
                        action=f"directive:{directive.intent}", owner_instruction_id=event.id,
                        ingress_bot_id=cfg.conversation_router_ingress_bot_id,
                    )
                task_id = event_task_id
                task = kb.get_task(conn, str(task_id))
                owner = str(getattr(task, "assignee", "") or "") if task else ""
                route = resolve_profile_route(ctx, owner=owner, config=cfg)
                return _routed_turn_result(
                    ctx=ctx, task_id=str(task_id), board_slug=resolved_board_slug,
                    event_id=event.id, route=route,
                    ingress_bot_id=cfg.conversation_router_ingress_bot_id,
                    directive=directive,
                )
            if cfg.conversation_router_enabled and not explicit:
                event = record_conversation_event(
                    mirror_conn, discord_message_id=ctx.message_id,
                    thread_id=ctx.thread_id, binding_key=None,
                    legacy_binding_key=str(task_id),
                    event_class="conversation.human", author_label=ctx.author_label,
                    content=ctx.content, replied_to_message_id=ctx.reply_to_message_id,
                    author_id=ctx.author_id, discord_created_at=ctx.discord_created_at,
                    discord_message_link=ctx.message_link, reply_context=ctx.reply_to_text,
                    attachments=ctx.attachments,
                )
                event_task_id = _event_bound_task_id(
                    mirror_conn, thread_id=ctx.thread_id,
                    binding_key=event.binding_key, legacy_task_id=str(task_id),
                )
                if event_task_id is None:
                    return KanbanReplyInboxResult(
                        consumed=True, reason="binding_unavailable", action="conversation",
                        owner_instruction_id=event.id,
                        ingress_bot_id=cfg.conversation_router_ingress_bot_id,
                    )
                task_id = event_task_id
                task = kb.get_task(conn, str(task_id))
                owner = str(getattr(task, "assignee", "") or "").strip()
                route = resolve_profile_route(ctx, owner=owner, config=cfg)
                ingress_bot_id = cfg.conversation_router_ingress_bot_id
                if route.profile is None or not ingress_bot_id:
                    reason = route.error or "ambiguous_owner"
                    return KanbanReplyInboxResult(
                        consumed=True, reason=reason, task_id=str(task_id),
                        action="conversation", owner_instruction_id=event.id,
                        ingress_bot_id=ingress_bot_id,
                        ack=(
                            "Rejected: mention exactly one configured profile bot."
                            if reason == "ambiguous_profile_mentions" else None
                        ),
                    )
                return KanbanReplyInboxResult(
                    consumed=False, reason="conversation_routed", task_id=str(task_id),
                    action="conversation", owner_instruction_id=event.id,
                    route_profile=route.profile,
                    route_profiles=route.profiles,
                    correlation_id="discord:" + hashlib.sha256(
                        f"{ctx.thread_id}\0{ctx.message_id}".encode("utf-8")
                    ).hexdigest(),
                    card_context=(
                        f"Kanban card {task_id} (board {resolved_board_slug}, "
                        f"target profile {route.profile}, route basis {route.basis})."
                    ),
                    ingress_bot_id=ingress_bot_id,
                )
            if log_enabled:
                return _handle_log_command(
                    conn,
                    mirror_conn,
                    task_id=str(task_id),
                    ctx=ctx,
                )
            if text_action is not None:
                return _handle_text_action(
                    conn,
                    mirror_conn,
                    task_id=str(task_id),
                    board_slug=resolved_board_slug,
                    ctx=ctx,
                    action=text_action,
                )
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
    replied_to_author_id = None
    replied_to_author_is_bot = False
    if reference is not None:
        raw_mid = getattr(reference, "message_id", None)
        reply_to_message_id = str(raw_mid) if raw_mid is not None else None
        resolved = getattr(reference, "resolved", None) or getattr(reference, "cached_message", None)
        reply_to_text = getattr(resolved, "content", None) if resolved is not None else None
        replied_author = getattr(resolved, "author", None)
        replied_to_author_id = str(getattr(replied_author, "id", "") or "") or None
        replied_to_author_is_bot = bool(getattr(replied_author, "bot", False))
    mentioned_user_ids = tuple(
        dict.fromkeys(
            str(getattr(mentioned, "id", "") or "")
            for mentioned in (getattr(message, "mentions", None) or ())
            if str(getattr(mentioned, "id", "") or "")
        )
    )
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
    created = getattr(message, "created_at", None)
    created_at = int(created.timestamp()) if created is not None else None
    attachments = tuple(
        {
            "id": str(getattr(item, "id", "") or ""),
            "filename": str(getattr(item, "filename", "") or ""),
            "url": str(getattr(item, "url", "") or ""),
            "content_type": getattr(item, "content_type", None),
            "size": getattr(item, "size", None),
        }
        for item in (getattr(message, "attachments", None) or ())
    )
    return DiscordReplyContext(
        message_id=message_id,
        author_id=author_id,
        author_label=author_label,
        forum_channel_id=forum_channel_id,
        thread_id=thread_id,
        content=content,
        reply_to_message_id=reply_to_message_id,
        reply_to_text=reply_to_text,
        mentioned_user_ids=mentioned_user_ids,
        replied_to_author_id=replied_to_author_id,
        replied_to_author_is_bot=replied_to_author_is_bot,
        discord_created_at=created_at,
        message_link=str(getattr(message, "jump_url", "") or "") or None,
        attachments=attachments,
    )


async def maybe_handle_discord_message(
    message: Any,
    *,
    config: KanbanReplyInboxConfig | None = None,
    mark_nonconversational=None,
    current_bot_id: str | None = None,
) -> KanbanReplyInboxResult:
    ctx = context_from_discord_message(message)
    if ctx is None:
        return KanbanReplyInboxResult(consumed=False, reason="not_thread_message")
    cfg = config or load_config()
    in_mirrored_forum = (
        cfg.enabled
        and cfg.conversation_router_enabled
        and bool(ctx.forum_channel_id)
        and ctx.forum_channel_id in cfg.forum_channel_ids
    )
    # Profile bots publish durable outbox events. Treat their mirrored copies as
    # already recorded output rather than creating a second ledger event/turn.
    if in_mirrored_forum and ctx.author_id in dict(cfg.profile_bot_user_ids):
        return KanbanReplyInboxResult(
            consumed=True, reason="profile_bot_output", action="conversation",
            ingress_bot_id=cfg.conversation_router_ingress_bot_id,
        )
    if (
        in_mirrored_forum
        and (
            not cfg.conversation_router_ingress_bot_id
            or str(current_bot_id or "") != cfg.conversation_router_ingress_bot_id
        )
    ):
        return KanbanReplyInboxResult(
            consumed=True,
            reason="not_ingress_bot",
            action="conversation",
            ingress_bot_id=cfg.conversation_router_ingress_bot_id,
        )
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


def handle_reaction_remove(
    ctx: DiscordReactionContext,
    *,
    config: KanbanReplyInboxConfig | None = None,
) -> KanbanReplyInboxResult:
    """Forget the active-delivery receipt so a later re-add becomes a new instruction."""
    cfg = config or load_config()
    if not cfg.enabled:
        return KanbanReplyInboxResult(consumed=False, reason="disabled")
    board_slug = cfg.board_slug or "default"
    resolved = resolve_thread_task(
        mirror_db_path(board_slug), forum_channel_id=ctx.forum_channel_id, thread_id=ctx.thread_id
    )
    if resolved is None:
        return KanbanReplyInboxResult(consumed=False, reason="unmapped_thread")
    task_id, resolved_board_slug = map(str, resolved)
    mirror_conn = connect_mirror(mirror_db_path(resolved_board_slug))
    try:
        ensure_receipts(mirror_conn)
        removed = mark_reaction_removed(mirror_conn, ctx.reaction_key)
    finally:
        mirror_conn.close()
    return KanbanReplyInboxResult(
        consumed=True,
        reason="reaction_removed" if removed else "no_active_receipt",
        task_id=task_id,
        action=f"reaction_removed:{ctx.intent}",
    )


def _reaction_ingress_rejection(
    cfg: KanbanReplyInboxConfig,
    ctx: DiscordReactionContext,
    current_bot_id: str | None,
) -> KanbanReplyInboxResult | None:
    if (
        not cfg.enabled
        or not cfg.conversation_router_enabled
        or not ctx.forum_channel_id
        or ctx.forum_channel_id not in cfg.forum_channel_ids
    ):
        return None
    ingress_bot_id = cfg.conversation_router_ingress_bot_id
    if ingress_bot_id and str(current_bot_id or "") == ingress_bot_id:
        return None
    return KanbanReplyInboxResult(
        consumed=True,
        reason="not_ingress_bot",
        action="reaction",
        ingress_bot_id=ingress_bot_id,
    )


async def maybe_handle_discord_reaction_remove(
    payload: Any,
    *,
    config: KanbanReplyInboxConfig | None = None,
    current_bot_id: str | None = None,
    resolved_channel: Any = _UNRESOLVED_CHANNEL,
) -> KanbanReplyInboxResult:
    ctx = context_from_discord_reaction(payload, resolved_channel=resolved_channel)
    if ctx is None:
        return KanbanReplyInboxResult(consumed=False, reason="unsupported_reaction")
    cfg = config or load_config()
    if ctx.forum_channel_id not in cfg.forum_channel_ids:
        return KanbanReplyInboxResult(consumed=False, reason="forum_not_configured")
    rejected = _reaction_ingress_rejection(cfg, ctx, current_bot_id)
    if rejected is not None:
        return rejected
    return await asyncio.to_thread(handle_reaction_remove, ctx, config=cfg)


async def maybe_handle_discord_reaction(
    payload: Any,
    *,
    config: KanbanReplyInboxConfig | None = None,
    current_bot_id: str | None = None,
    resolved_channel: Any = _UNRESOLVED_CHANNEL,
) -> KanbanReplyInboxResult:
    ctx = context_from_discord_reaction(payload, resolved_channel=resolved_channel)
    if ctx is None:
        return KanbanReplyInboxResult(consumed=False, reason="unsupported_reaction")
    cfg = config or load_config()
    if ctx.forum_channel_id not in cfg.forum_channel_ids:
        return KanbanReplyInboxResult(consumed=False, reason="forum_not_configured")
    rejected = _reaction_ingress_rejection(cfg, ctx, current_bot_id)
    if rejected is not None:
        return rejected
    try:
        return await asyncio.to_thread(handle_reaction, ctx, config=cfg)
    except ValueError as exc:
        return KanbanReplyInboxResult(consumed=True, reason="rejected", ack=f"Rejected: {exc}")
