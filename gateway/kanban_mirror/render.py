"""Pure renderer for the Kanban Discord mirror.

Turns an ``Initiative`` + its ``BoardSnapshot`` slice into Discord-forum-post
markdown. No I/O, no Discord client, no board writes — just
``(initiative, cards, prose) -> markdown`` so it's trivially unit-testable
and reusable from both the planner and the daemon.

Secret-redaction patterns, ``branch_display`` conventions, the
``needs_brian`` keyword heuristic, and ``STATUS_ORDER`` are lifted from the
v1 script (``discord_forum_mirror.py``) to keep display conventions stable
across the rewrite.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from gateway.kanban_mirror.state import BoardSnapshot, Card, Initiative, is_terminal

# ---------------------------------------------------------------------------
# Lifted from v1: discord_forum_mirror.py
# ---------------------------------------------------------------------------

STATUS_ORDER = {
    "running": 0,
    "blocked": 1,
    "review": 2,
    "ready": 3,
    "todo": 4,
    "scheduled": 5,
    "triage": 6,
    "done": 90,
    "archived": 91,
    "skipped": 92,
    "canceled": 92,
    "cancelled": 92,
}

STATUS_EMOJI: dict[str, str] = {
    "done": "✅",
    "archived": "✅",
    "running": "\U0001F7E2",
    "review": "\U0001F7E1",
    "blocked": "\U0001F534",
    "skipped": "⏭️",
    "canceled": "⏭️",
    "cancelled": "⏭️",
}

_SECRET_PATTERNS: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
    (
        re.compile(
            r"(?i)(bot\s+token|discord[_-]?token|api[_-]?key|secret|password|passwd|authorization)"
            r"\s*[:=]\s*(?:(?:Bot|Bearer)\s+)?([^\s,;]+)"
        ),
        lambda m: f"{m.group(1)}=[REDACTED]",
    ),
    (
        re.compile(r"(?i)\b(Bot|Bearer)\s+[A-Za-z0-9._~+/-]{12,}"),
        lambda m: f"{m.group(1)} [REDACTED]",
    ),
    (
        re.compile(r"\b[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,}\b"),
        lambda m: "[REDACTED_DISCORD_TOKEN]",
    ),
]

_NEEDS_BRIAN_WORDS = ("brian", "credential", "permission", "authorize", "approval")

_MAX_WORK_ITEM_LINES = 12
_MEDIA_LINE_RE = re.compile(r"(?im)^[ \t>*-]*(?:\[\[audio_as_voice\]\][ \t]*)?MEDIA:\s*(?P<path>\S+)\s*$")


def _s(value: str | int | None) -> str:
    """Coerce a Card field (sqlite columns are ``str | int | None``) to str."""
    return "" if value is None else str(value)


def _i(value: str | int | None) -> int:
    """Coerce a Card field to int, defaulting to 0 on anything unparsable."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def redact(text: str) -> str:
    """Strip secret-shaped substrings and shorten home-dir paths.

    Lifted from v1's ``redact_and_truncate`` — this wrapper drops the
    ``notices``/``max_chars`` truncation half (that's handled once, at the
    end of ``render_post``, so the footer line never gets cut) and just
    returns the cleaned text.
    """
    value = _s(text)
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def branch_display(card: Card) -> str | None:
    branch = _s(card.branch_name).strip()
    if not branch:
        return None
    if branch.startswith("<") and branch.endswith(">"):
        return branch
    if branch == "brian/main":
        return f"`{branch}` (main)"
    if card.workspace_kind == "worktree":
        return f"`{branch}` (worktree)"
    return f"`{branch}`"


def _needs_brian_keywords(*texts: str | int | None) -> bool:
    combined = "\n".join(_s(t) for t in texts).lower()
    return any(word in combined for word in _NEEDS_BRIAN_WORDS)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _emoji(status: str | int | None) -> str:
    return STATUS_EMOJI.get(_s(status).strip().lower(), "▫️")


def _sort_key(card: Card) -> tuple[int, int]:
    status = _s(card.status).strip().lower()
    priority = _i(card.priority)
    return (STATUS_ORDER.get(status, 50), -priority)


def _expand_with_children(member_cards: list[Card], snapshot: BoardSnapshot) -> list[Card]:
    """member cards plus their direct children (as known to the snapshot)."""
    seen: dict[str, Card] = {}
    for card in member_cards:
        seen[card.id] = card
        for child_id in snapshot.children.get(card.id, []):
            child = snapshot.cards.get(child_id)
            if child is not None:
                seen.setdefault(child.id, child)
    return list(seen.values())


def stage_tag(member_cards: list[Card], snapshot: BoardSnapshot) -> str:
    """running|review|waiting|done, per Global Constraints precedence."""
    statuses = [_s(c.status).strip().lower() for c in _expand_with_children(member_cards, snapshot)]
    if any(s == "running" for s in statuses):
        return "running"
    if any(s == "review" for s in statuses):
        return "review"
    if any(not is_terminal(s) for s in statuses):
        return "waiting"
    return "done"


def needs_brian_tag(member_cards: list[Card], snapshot: BoardSnapshot) -> bool:
    """Any member-or-child in review, or blocked with a needs-Brian keyword hit."""
    for card in _expand_with_children(member_cards, snapshot):
        status = _s(card.status).strip().lower()
        if status == "review":
            return True
        if status == "blocked" and _needs_brian_keywords(
            card.title, card.body, card.result, card.last_failure_error
        ):
            return True
    return False


def _primary_card(member_cards: list[Card]) -> Card | None:
    if not member_cards:
        return None
    non_terminal = [c for c in member_cards if not is_terminal(_s(c.status))]
    pool = non_terminal or member_cards
    return max(pool, key=lambda c: _i(c.priority))


def primary_assignee(member_cards: list[Card]) -> str | None:
    """Assignee of highest-priority non-terminal member (fallback: any member)."""
    card = _primary_card(member_cards)
    return _s(card.assignee) or None if card is not None else None


def post_title(initiative: Initiative, snapshot: BoardSnapshot) -> str:
    """Initiative title, <=100 chars, no status prefix/id.

    Redacted: this is the only place thread names are produced (daemon's
    ``create_thread``/``edit_post`` ops and ``rebuild`` both derive the
    Discord thread name from this function's output), so redacting here
    covers all outbound thread-name text centrally.
    """
    title = redact(_s(initiative.title))
    if len(title) <= 100:
        return title
    return title[:96].rstrip() + "…"


# ---------------------------------------------------------------------------
# Body rendering
# ---------------------------------------------------------------------------


def _split_first_sentence(brief: str) -> str:
    match = re.match(r"(.+?[.!?])(\s+|$)", brief, re.S)
    if match:
        first, rest = match.group(1), brief[match.end(1):]
    else:
        first, rest = brief, ""
    return f"**{first}**{rest}"


def _finished_for_display(status: str | int | None) -> bool:
    return is_terminal(_s(status)) or _s(status).strip().lower() in {"skipped", "canceled", "cancelled"}


@dataclass(frozen=True)
class WorkItem:
    card: Card
    indented: bool = False
    wait_parent_ids: tuple[str, ...] = ()


def _work_items(member_ids: list[str], snapshot: BoardSnapshot) -> list[WorkItem]:
    """Flat dependency-DAG list for Discord.

    Parent→child links are dependency edges, not epic containment. Render a
    shallow, readable topological list: the main chain stays unindented;
    children are indented only when a parent fans out to sibling children that
    may run in parallel after that parent completes.
    """
    items: list[WorkItem] = []
    emitted: set[str] = set()
    visiting: set[str] = set()
    reachable: set[str] = set()

    def mark_reachable(task_id: str) -> None:
        if task_id in reachable or task_id not in snapshot.cards:
            return
        reachable.add(task_id)
        for cid in snapshot.children.get(task_id, []):
            mark_reachable(cid)

    for root_id in member_ids:
        mark_reachable(root_id)

    def emit(task_id: str, *, indented: bool) -> None:
        if task_id in emitted or task_id in visiting:
            return
        card = snapshot.cards.get(task_id)
        if card is None:
            return
        parents = snapshot.parents.get(task_id, [])
        if any(pid in reachable and pid not in emitted for pid in parents if pid in snapshot.cards):
            return

        visiting.add(task_id)
        wait_parent_ids = tuple(parents) if len(parents) > 1 else ()
        items.append(WorkItem(card=card, indented=indented, wait_parent_ids=wait_parent_ids))
        emitted.add(task_id)

        children = [cid for cid in snapshot.children.get(task_id, []) if cid in snapshot.cards]
        children.sort(key=lambda cid: _sort_key(snapshot.cards[cid]))
        child_indent = len(children) > 1
        for cid in children:
            emit(cid, indented=child_indent)
        visiting.discard(task_id)

    for root_id in member_ids:
        emit(root_id, indented=False)

    # Fan-in cards skipped on the first parent become eligible after a later
    # sibling emits; iterate boundedly until no more known descendants can land.
    changed = True
    while changed:
        before = len(items)
        for item in list(items):
            children = [cid for cid in snapshot.children.get(item.card.id, []) if cid in snapshot.cards]
            children.sort(key=lambda cid: _sort_key(snapshot.cards[cid]))
            child_indent = len(children) > 1
            for cid in children:
                emit(cid, indented=child_indent)
        changed = len(items) != before

    return items


def _media_paths_from_text(text: str | int | None) -> list[str]:
    paths: list[str] = []
    for match in _MEDIA_LINE_RE.finditer(_s(text)):
        path = match.group("path").strip().strip("`\"'").rstrip(".,;:)}]")
        if path:
            paths.append(path)
    return paths


def review_artifact_paths(member_cards: list[Card], snapshot: BoardSnapshot) -> list[str]:
    """Collect MEDIA paths from review-stage cards and their nearby evidence."""
    paths: list[str] = []
    seen: set[str] = set()
    for card in _expand_with_children(member_cards, snapshot):
        if _s(card.status).strip().lower() != "review":
            continue
        text_sources: list[str | int | None] = [card.body, card.result, card.last_failure_error]
        text_sources.extend(c.get("body") for c in snapshot.recent_comments.get(card.id, []))
        for text in text_sources:
            for path in _media_paths_from_text(text):
                if path in seen:
                    continue
                seen.add(path)
                paths.append(path)
    return paths


def review_artifacts_block(member_cards: list[Card], snapshot: BoardSnapshot) -> str | None:
    paths = review_artifact_paths(member_cards, snapshot)
    if not paths:
        return None
    lines = ["**Review artifacts**"]
    lines.extend(f"• {Path(path).name or path}" for path in paths)
    return "\n".join(lines)


def _fold_items(items: list[WorkItem]) -> tuple[list[WorkItem], str | None]:
    """Cap the already-ordered DAG display at 12 lines with a fold tail.

    Finished items fold first (``… N more done``). If even the active items
    overflow the cap, preserve the visible DAG order for the first 11 active
    items and account for everything hidden in the tail.
    """
    non_done = [item for item in items if not _finished_for_display(item.card.status)]
    done = [item for item in items if _finished_for_display(item.card.status)]

    if len(non_done) + len(done) <= _MAX_WORK_ITEM_LINES:
        return items, None

    item_budget = _MAX_WORK_ITEM_LINES - 1  # reserve one line for the tail
    if len(non_done) > item_budget:
        shown: list[WorkItem] = []
        hidden_active = 0
        for item in items:
            if _finished_for_display(item.card.status):
                continue
            if len(shown) < item_budget:
                shown.append(item)
            else:
                hidden_active += 1
        tail = f"… {hidden_active} more active"
        if done:
            tail += f", {len(done)} done"
        return shown, tail

    shown: list[WorkItem] = []
    shown_done = 0
    done_budget = item_budget - len(non_done)
    for item in items:
        if not _finished_for_display(item.card.status):
            shown.append(item)
        elif shown_done < done_budget:
            shown.append(item)
            shown_done += 1
    folded_done = len(done) - shown_done
    return shown, f"… {folded_done} more done"


def _format_item(item: WorkItem, initiative: Initiative, snapshot: BoardSnapshot) -> str:
    card = item.card
    line = f"{_emoji(card.status)} {redact(_s(card.title))}"
    if item.indented:
        line = "  " + line
    if item.wait_parent_ids:
        parent_titles = [
            redact(_s(snapshot.cards[parent_id].title))
            for parent_id in item.wait_parent_ids
            if parent_id in snapshot.cards
        ]
        if parent_titles:
            line += f" — waits on: {', '.join(parent_titles)}"
    status = _s(card.status).strip().lower()
    if status == "blocked":
        reason = initiative.blocked_reasons.get(card.id)
        if reason:
            line += f" — *{redact(reason)}*"
    return line


def _relative_time(now: int, ts: int | None) -> str:
    if ts is None:
        return "just now"
    delta = max(0, (now or 0) - ts)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _footer(initiative: Initiative, member_cards: list[Card], member_ids: list[str], now: int) -> str:
    ref = _primary_card(member_cards)
    assignee = (_s(ref.assignee) if ref is not None else "") or "unassigned"
    priority = _i(ref.priority) if ref is not None else 0
    branch = branch_display(ref) if ref is not None else None
    if branch:
        # The whole footer is one code span; inner backticks (from
        # branch_display's own fencing) would close it early in Discord.
        branch = branch.replace("`", "")
    rel = _relative_time(now, initiative.brief_updated_at)

    segments = [assignee, f"P{priority}", ",".join(member_ids)]
    if branch:
        segments.append(f"branch {branch}")
    segments.append(f"updated {rel}")
    return "`" + " · ".join(segments) + "`"


def _truncate(body: str, max_chars: int) -> str:
    """Drop lines on a line boundary, from the bottom up, keeping the footer."""
    if len(body) <= max_chars:
        return body
    lines = body.split("\n")
    footer = lines[-1]
    content = lines[:-1]
    while content and len("\n".join(content + [footer])) > max_chars:
        content.pop()
    while content and content[-1] == "":
        content.pop()
    return "\n".join(content + [footer]) if content else footer


def render_post(initiative: Initiative, snapshot: BoardSnapshot, max_chars: int, now: int) -> str:
    member_ids = list(initiative.members.keys())
    member_cards = [snapshot.cards[m] for m in member_ids if m in snapshot.cards]

    brief = (initiative.brief or "").strip()
    needs_you_line: str | None = None
    if not brief:
        first_para = f"**{redact(_s(initiative.title))}**"
    else:
        first_para = _split_first_sentence(redact(brief))
        if initiative.needs_you:
            needs_you_line = f"⚠️ **Needs you:** {redact(initiative.needs_you)}"

    items = _work_items(member_ids, snapshot)
    shown, fold_tail = _fold_items(items)
    item_lines = [_format_item(item, initiative, snapshot) for item in shown]
    if fold_tail:
        item_lines.append(fold_tail)

    work_block = "**Work items**"
    if item_lines:
        work_block += "\n" + "\n".join(item_lines)

    footer = _footer(initiative, member_cards, member_ids, now)

    review_block = review_artifacts_block(member_cards, snapshot)

    parts = [first_para]
    if review_block:
        parts.append(review_block)
    if needs_you_line:
        parts.append(needs_you_line)
    parts.append(work_block)
    parts.append(footer)
    body = "\n\n".join(parts)
    return _truncate(body, max_chars)


def render_digest(
    demoted_roots: list[Card], snapshot: BoardSnapshot, done_this_week: int, max_chars: int
) -> str:
    """Weekly digest body: demoted (idle/archived-off-post) roots + a done tally."""
    lines = ["**Weekly digest**", ""]
    if demoted_roots:
        lines.append("**Moved off active posts**")
        for card in sorted(demoted_roots, key=_sort_key):
            lines.append(f"{_emoji(card.status)} {redact(_s(card.title))}")
        lines.append("")
    lines.append(f"Completed this week: {done_this_week}")
    body = "\n".join(lines).rstrip("\n")
    return _truncate(body, max_chars)
