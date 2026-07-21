"""LLM writer for the Kanban Discord mirror.

The only LLM-touching layer in the mirror: plain completion calls via
``agent.auxiliary_client.async_call_llm`` with ``task="kanban_mirror"``
(model configured under ``auxiliary.kanban_mirror`` in config.yaml, falling
back to the auto provider chain when unset). No tools, no agent session, no
Discord I/O — the daemon (Task 7) calls this module and owns everything
else.

Every public function is ``async`` and raises ``WriterError`` on *any*
failure (LLM error, empty content, JSON parse failure, empty brief) so the
daemon can uniformly keep the old prose / retry with backoff / fall back to
the deterministic ``assign_default`` curation.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from plugins.platforms.discord.kanban_mirror.render import redact
from plugins.platforms.discord.kanban_mirror.state import BoardSnapshot, Card, Initiative

# ---------------------------------------------------------------------------
# Errors / results
# ---------------------------------------------------------------------------


class WriterError(Exception):
    """Raised on any LLM/parse/validation failure. Daemon keeps old prose."""


@dataclass
class ProseResult:
    brief: str
    needs_you: str | None
    blocked_reasons: dict[str, str]


@dataclass
class CurationDecision:
    task_id: str
    action: str  # "own_post" | "join" | "digest"
    initiative_id: str | None
    title: str | None


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PROSE_SYSTEM = """You write status briefs for a human reviewing his AI agents' work on a Discord board.
Write plainly. Never use task ids, JSON, or agent jargon. The reader has not seen the underlying tickets.
Reply with ONLY a JSON object: {"brief": "...", "needs_you": "..." or null, "blocked_reasons": {"<task_id>": "..."}}
- brief: 1-3 sentences. What this workstream is and where it stands right now.
- needs_you: null unless the human must act; then ONE imperative sentence saying exactly what to decide/do and where.
- blocked_reasons: for each blocked card given, a clause under 10 words explaining the blockage in human terms.
- If docs or artifacts need user review, say that they are attached to the Discord thread. When naming them in the thread body, use plain file names or descriptions, never MEDIA: tags."""

NOTE_SYSTEM = """You write one short progress note (1-2 sentences) for a Discord thread a human skims.
Plain language, no ids, no jargon. State what just happened and what happens next if known.
Reply with ONLY the note text."""

CURATE_SYSTEM = """You group new kanban root cards into human-facing Discord initiative posts.
Each card gets exactly one decision. Prefer "own_post" for meaningful workstreams; "join" an existing
initiative only when the card is clearly part of that same effort; "digest" for trivial/housekeeping cards.
For "join", initiative_id must be one of the existing initiative ids listed in the input. To group
several cards from THIS batch together, mark one of them "own_post" and have the others "join" it
using initiative_id "init_<that card's task_id>". Any other initiative_id is invalid.
Reply with ONLY a JSON array: [{"task_id": "...", "action": "own_post"|"join"|"digest",
"initiative_id": "<required for join>", "title": "<short human title, required for own_post>"}]"""

# ---------------------------------------------------------------------------
# Guardrail helpers
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")


def _strip_fences(text: str) -> str:
    """Strip a leading/trailing markdown code fence, if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _FENCE_RE.sub("", stripped).strip()
    return stripped


def _truncate_sentence(text: str, limit: int) -> str:
    """Truncate ``text`` to at most ``limit`` chars, at a sentence boundary.

    Finds the last sentence-ending punctuation at or before ``limit`` and
    cuts there. Falls back to a hard character cut with an ellipsis if no
    sentence boundary exists within the limit.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    last_end = None
    for match in _SENTENCE_END_RE.finditer(window):
        last_end = match.end(0)
    if last_end:
        return window[:last_end].rstrip()
    # No sentence boundary within the limit — hard cut.
    if limit <= 1:
        return window[:limit]
    return window[: limit - 1].rstrip() + "…"


def _parse_json(content: str) -> object:
    cleaned = _strip_fences(content)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise WriterError(f"failed to parse JSON from LLM response: {exc}") from exc


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


async def _call(system: str, user: str, max_tokens: int = 700, timeout: int = 60) -> str:
    from agent.auxiliary_client import async_call_llm

    try:
        resp = await async_call_llm(
            task="kanban_mirror",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.3,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    except Exception as exc:
        raise WriterError(f"LLM call failed: {exc}") from exc
    try:
        content = resp.choices[0].message.content or ""
    except Exception as exc:
        raise WriterError(f"malformed LLM response: {exc}") from exc
    if not content.strip():
        raise WriterError("empty LLM response")
    return content.strip()


# ---------------------------------------------------------------------------
# Context pack builder
# ---------------------------------------------------------------------------


def _s(value: str | int | None) -> str:
    return "" if value is None else str(value)


def _card_pack(card: Card, snapshot: BoardSnapshot) -> dict:
    comments = snapshot.recent_comments.get(card.id, [])[-5:]
    events = snapshot.recent_events.get(card.id, [])[-5:]
    return {
        "task_id": card.id,
        "title": _s(card.title),
        "status": _s(card.status),
        "priority": _s(card.priority),
        "assignee": _s(card.assignee),
        "body": _s(card.body)[:1500],
        "comments": [
            {"author": _s(c.get("author")), "body": _s(c.get("body"))[:300]}
            for c in comments
        ],
        "events": [
            {"kind": _s(e.get("kind")), "payload": _s(e.get("payload"))[:300]}
            for e in events
        ],
        "last_failure_error": _s(card.last_failure_error) or None,
    }


def _build_context_pack(cards: list[Card], snapshot: BoardSnapshot, previous_brief: str | None = None) -> str:
    pack: dict = {"cards": [_card_pack(c, snapshot) for c in cards]}
    if previous_brief:
        pack["previous_brief"] = previous_brief
    raw = json.dumps(pack, indent=2, ensure_ascii=False)
    return redact(raw)


def _initiative_cards(initiative: Initiative, snapshot: BoardSnapshot) -> list[Card]:
    cards: list[Card] = []
    for task_id in initiative.members:
        card = snapshot.cards.get(task_id)
        if card is not None:
            cards.append(card)
    return cards


# ---------------------------------------------------------------------------
# write_prose
# ---------------------------------------------------------------------------


async def write_prose(initiative: Initiative, snapshot: BoardSnapshot) -> ProseResult:
    member_cards = _initiative_cards(initiative, snapshot)
    context_pack = _build_context_pack(member_cards, snapshot, previous_brief=initiative.brief)
    user = (
        f"Initiative: {initiative.title}\n\n"
        f"Context pack (JSON):\n{context_pack}"
    )
    content = await _call(PROSE_SYSTEM, user)
    parsed = _parse_json(content)
    if not isinstance(parsed, dict):
        raise WriterError("prose response was not a JSON object")

    brief = _s(parsed.get("brief")).strip()
    if not brief:
        raise WriterError("prose response had an empty brief")
    brief = _truncate_sentence(brief, 500)

    needs_you_raw = parsed.get("needs_you")
    needs_you: str | None = None
    if needs_you_raw is not None:
        needs_you_str = _s(needs_you_raw).strip()
        if needs_you_str:
            needs_you = _truncate_sentence(needs_you_str, 200)

    blocked_reasons_raw = parsed.get("blocked_reasons") or {}
    if not isinstance(blocked_reasons_raw, dict):
        raise WriterError("blocked_reasons was not a JSON object")
    blocked_reasons: dict[str, str] = {}
    for task_id, reason in blocked_reasons_raw.items():
        reason_str = _s(reason).strip()
        if reason_str:
            blocked_reasons[str(task_id)] = _truncate_sentence(reason_str, 80)

    return ProseResult(brief=brief, needs_you=needs_you, blocked_reasons=blocked_reasons)


# ---------------------------------------------------------------------------
# write_note
# ---------------------------------------------------------------------------


async def write_note(
    initiative: Initiative,
    snapshot: BoardSnapshot,
    note_kind: str,
    task_id: str,
    char_limit: int,
) -> str:
    card = snapshot.cards.get(task_id)
    cards_for_pack = [card] if card is not None else []
    context_pack = _build_context_pack(cards_for_pack, snapshot)
    user = (
        f"Note kind: {note_kind}\n"
        f"Triggering task: {task_id}\n"
        f"Initiative brief (for continuity): {initiative.brief or '(none yet)'}\n\n"
        f"Context pack (JSON):\n{context_pack}"
    )
    content = await _call(NOTE_SYSTEM, user, max_tokens=200)
    note = _strip_fences(content).strip()
    if not note:
        raise WriterError("note response was empty")
    return _truncate_sentence(note, char_limit)


# ---------------------------------------------------------------------------
# curate
# ---------------------------------------------------------------------------

_VALID_ACTIONS = {"own_post", "join", "digest"}


async def curate(
    unassigned_roots: list[Card],
    initiatives: dict[str, Initiative],
    snapshot: BoardSnapshot,
) -> list[CurationDecision]:
    if not unassigned_roots:
        return []

    existing_initiatives = [
        {"initiative_id": init.id, "title": init.title}
        for init in initiatives.values()
    ]
    context_pack = _build_context_pack(unassigned_roots, snapshot)
    user = (
        f"Existing initiatives (JSON): {json.dumps(existing_initiatives, ensure_ascii=False)}\n\n"
        f"New root cards - context pack (JSON):\n{context_pack}"
    )
    # Scale the output budget with batch size: each decision object costs
    # ~40-100 tokens and a cutover-sized batch (20+ roots) blows well past
    # the 700-token default used for prose/notes, silently truncating the
    # JSON array.
    content = await _call(
        CURATE_SYSTEM, user, max_tokens=max(3000, 200 * len(unassigned_roots)), timeout=120
    )
    try:
        parsed = _parse_json(content)
    except WriterError as exc:
        if not _strip_fences(content).rstrip().endswith("]"):
            raise WriterError(
                f"{exc}; response likely truncated (max_tokens)"
            ) from exc
        raise
    if not isinstance(parsed, list):
        raise WriterError("curate response was not a JSON array")

    valid_task_ids = {c.id for c in unassigned_roots}
    decisions: list[CurationDecision] = []
    seen_task_ids: set[str] = set()

    # A "join" may target an initiative created by an "own_post" decision
    # for another task earlier/later in this SAME batch (id `init_<task_id>`)
    # — the daemon processes own_post decisions first so these resolve, even
    # though neither initiative exists in ``initiatives`` yet at curate time.
    batch_own_post_ids = {
        f"init_{_s(entry.get('task_id')).strip()}"
        for entry in parsed
        if isinstance(entry, dict) and _s(entry.get("action")).strip() == "own_post"
        and _s(entry.get("task_id")).strip()
    }

    for entry in parsed:
        if not isinstance(entry, dict):
            raise WriterError("curate entry was not a JSON object")
        task_id = _s(entry.get("task_id")).strip()
        action = _s(entry.get("action")).strip()
        initiative_id = entry.get("initiative_id")
        title = entry.get("title")
        initiative_id = _s(initiative_id).strip() or None
        title = _s(title).strip() or None

        if task_id not in valid_task_ids:
            raise WriterError(f"curate response referenced unknown task_id {task_id!r}")
        if action not in _VALID_ACTIONS:
            raise WriterError(f"curate response had unknown action {action!r} for {task_id}")
        if action == "join":
            if not initiative_id or (
                initiative_id not in initiatives and initiative_id not in batch_own_post_ids
            ):
                raise WriterError(
                    f"curate 'join' decision for {task_id} missing/unknown initiative_id"
                )
        if action == "own_post" and not title:
            raise WriterError(f"curate 'own_post' decision for {task_id} missing title")

        seen_task_ids.add(task_id)
        decisions.append(
            CurationDecision(
                task_id=task_id,
                action=action,
                initiative_id=initiative_id,
                title=title,
            )
        )

    missing = valid_task_ids - seen_task_ids
    if missing:
        raise WriterError(f"curate response missing decisions for: {sorted(missing)}")

    return decisions
