"""Pure planner for the Kanban Discord mirror.

Turns a ``BoardSnapshot`` + persisted mirror ``state`` into a list of ``Op``s
for the daemon (Task 7) to execute. No sqlite, no network, no config reads
beyond the ``cfg`` passed in — everything the planner needs arrives as
arguments so it stays trivially unit-testable.

The LLM writer (curation, prose) lives downstream of this module: the
planner only ever emits the *structural* op (``curate``, ``post_note`` with
a note kind) — turning that into actual grouping decisions or prose text is
the daemon/writer's job.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from gateway.kanban_mirror.config import MirrorConfig
from gateway.kanban_mirror.render import (
    needs_brian_tag,
    post_title,
    primary_assignee,
    render_digest,
    render_post,
    stage_tag,
)
from gateway.kanban_mirror.state import (
    BoardSnapshot,
    Card,
    Initiative,
    is_terminal,
    material_sig,
)

_SEVEN_DAYS = 7 * 86400


@dataclass(frozen=True)
class Op:
    kind: str
    data: dict


# ---------------------------------------------------------------------------
# Tag / hash helpers shared between plan() and current_publish_hash()
# ---------------------------------------------------------------------------


def _member_cards(initiative: Initiative, snapshot: BoardSnapshot) -> list[Card]:
    return [snapshot.cards[m] for m in initiative.members if m in snapshot.cards]


def _tags_for(initiative: Initiative, snapshot: BoardSnapshot) -> list[str]:
    member_cards = _member_cards(initiative, snapshot)
    tags: list[str] = []
    if needs_brian_tag(member_cards, snapshot):
        tags.append("needs-brian")
    tags.append(stage_tag(member_cards, snapshot))
    assignee = primary_assignee(member_cards)
    if assignee:
        tags.append(assignee)
    return tags


def current_publish_hash(initiative: Initiative, snapshot: BoardSnapshot, cfg: MirrorConfig) -> str:
    """sha256 over rendered title + body + tags, for change detection.

    Rendered with the initiative's ``brief_updated_at`` (or 0) as the
    "now" passed to ``render_post`` — NOT wall-clock now. ``render_post``'s
    footer includes a relative "updated Xm ago" string that drifts with
    wall-clock time; if we hashed against real "now" the hash would change
    on every planner tick even when nothing material changed, forcing a
    spurious ``edit_post`` every poll. The daemon calls this same function
    right after publishing to compute the ``published_hash`` it stores, so
    using a stable "now" here is what keeps the comparison meaningful across
    ticks.
    """
    stable_now = initiative.brief_updated_at or 0
    title = post_title(initiative, snapshot)
    body = render_post(initiative, snapshot, cfg.max_post_chars, stable_now)
    tags = _tags_for(initiative, snapshot)
    payload = "|".join([title, body, ",".join(tags)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _digest_hash(title: str, body: str) -> str:
    return hashlib.sha256(f"{title}|{body}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def _all_member_ids(state: dict[str, Initiative], digest: Initiative | None) -> set[str]:
    """Task ids already curated into a live (non-archived) initiative.

    Archived initiatives are deliberately excluded: once an initiative is
    archived (e.g. all members reached terminal status and the thread was
    closed out), a member card that later reopens to a non-terminal status
    must be treated as unassigned again so it gets re-curated into a fresh
    initiative rather than silently vanishing.
    """
    seen: set[str] = set()
    for initiative in state.values():
        if initiative.archived_at is not None:
            continue
        seen.update(initiative.members.keys())
    if digest is not None:
        seen.update(digest.members.keys())
    return seen


def _unassigned_curate_op(snapshot: BoardSnapshot, state: dict[str, Initiative],
                            digest: Initiative | None) -> Op | None:
    assigned = _all_member_ids(state, digest)
    unassigned = sorted(
        card.id for card in snapshot.active_roots() if card.id not in assigned
    )
    if not unassigned:
        return None
    return Op("curate", {"task_ids": unassigned})


def _previous_card(card: Card, last_status: str | None) -> Card:
    """A copy of ``card`` with its status swapped to the last-seen value.

    Used only to recompute ``needs_brian_tag`` as it would have evaluated
    before the most recent status change, so we can detect a false->true
    flip (initiative newly blocked).
    """
    return replace(card, status=last_status)


def _blocked_note_op(initiative: Initiative, snapshot: BoardSnapshot,
                      member_cards: list[Card], note_keys: set[str], now: int) -> Op | None:
    current_flag = needs_brian_tag(member_cards, snapshot)
    if not current_flag:
        return None
    previous_cards = [
        _previous_card(card, initiative.members[card.id].last_status)
        for card in member_cards
    ]
    previous_flag = needs_brian_tag(previous_cards, snapshot)

    if not previous_flag:
        # Newly blocked this tick: isolate the triggering member for a
        # precisely-attributed note.
        triggering_task_id = None
        triggering_status = None
        for card in sorted(member_cards, key=lambda c: c.id):
            member = initiative.members[card.id]
            single_current = needs_brian_tag([card], snapshot)
            single_previous = needs_brian_tag([_previous_card(card, member.last_status)], snapshot)
            if single_current and not single_previous:
                triggering_task_id = card.id
                triggering_status = card.status
                break
        if triggering_task_id is None:
            # Couldn't isolate a single trigger (e.g. simultaneous flips) —
            # fall back to a timestamp so the key is still deterministic
            # per-tick.
            triggering_task_id = sorted(c.id for c in member_cards)[0] if member_cards else "unknown"
            triggering_status = now

        note_key = f"blocked:{triggering_task_id}:{triggering_status}"
        if note_key not in note_keys:
            return Op("post_note", {
                "initiative_id": initiative.id,
                "note_key": note_key,
                "note_kind": "initiative_blocked",
                "task_id": triggering_task_id,
            })
        return None

    # Belt-and-braces retry: no flip detected this tick — typically because
    # ``member_seen`` already advanced ``last_status`` past the transition
    # on a prior tick whose note send then failed (member_seen commits
    # unconditionally; the note only commits on a 2xx). If a currently
    # needs-Brian member has no recorded note for its current status, retry
    # it rather than losing the note forever.
    for card in sorted(member_cards, key=lambda c: c.id):
        if needs_brian_tag([card], snapshot):
            note_key = f"blocked:{card.id}:{card.status}"
            if note_key not in note_keys:
                return Op("post_note", {
                    "initiative_id": initiative.id,
                    "note_key": note_key,
                    "note_kind": "initiative_blocked",
                    "task_id": card.id,
                })
    return None


def _plan_initiative(initiative: Initiative, snapshot: BoardSnapshot,
                      note_keys: set[str], cfg: MirrorConfig, now: int) -> list[Op]:
    ops: list[Op] = []
    member_cards = _member_cards(initiative, snapshot)

    # 1. stale material -> mark_stale + member_seen (per member, sig changed)
    for card in member_cards:
        member = initiative.members[card.id]
        child_statuses = [
            snapshot.cards[c].status
            for c in snapshot.children.get(card.id, [])
            if c in snapshot.cards
        ]
        sig = material_sig(card, child_statuses)
        if sig != member.last_sig:
            ops.append(Op("mark_stale", {"initiative_id": initiative.id}))
            ops.append(Op("member_seen", {"task_id": card.id, "status": card.status, "sig": sig}))

    # 2. member terminal -> post_note member_done (retried every tick until
    #    note_keys shows it landed; NOT gated on last_status having just
    #    transitioned, since member_seen advances unconditionally each tick
    #    while the note only commits after a Discord 2xx — gating on the
    #    transition would mean a failed send is never retried once
    #    last_status catches up). Suppress these once the whole initiative is
    #    terminal: the closure mechanism is now done-tag + idle archive, not an
    #    in-thread closure acknowledgement.
    all_terminal = bool(member_cards) and all(is_terminal(str(c.status or "")) for c in member_cards)
    if not all_terminal:
        for card in member_cards:
            if is_terminal(str(card.status or "")):
                note_key = f"done:{card.id}"
                if note_key not in note_keys:
                    ops.append(Op("post_note", {
                        "initiative_id": initiative.id,
                        "note_key": note_key,
                        "note_kind": "member_done",
                        "task_id": card.id,
                    }))

    # 3. initiative newly blocked -> post_note initiative_blocked
    blocked_op = _blocked_note_op(initiative, snapshot, member_cards, note_keys, now)
    if blocked_op is not None:
        ops.append(blocked_op)

    # 4. all members terminal -> apply the done tag promptly, then let the
    #    daemon archive only after the configured thread-idle window.
    if all_terminal:
        if initiative.thread_id is not None:
            title = post_title(initiative, snapshot)
            body = render_post(initiative, snapshot, cfg.max_post_chars, now)
            tags = _tags_for(initiative, snapshot)
            new_hash = current_publish_hash(initiative, snapshot, cfg)
            if new_hash != initiative.published_hash:
                ops.append(Op("edit_post", {
                    "initiative_id": initiative.id, "title": title, "body": body, "tags": tags,
                }))
        ops.append(Op("archive_thread", {"initiative_id": initiative.id}))
        return ops

    # 5. thread create/edit
    title = post_title(initiative, snapshot)
    body = render_post(initiative, snapshot, cfg.max_post_chars, now)
    tags = _tags_for(initiative, snapshot)
    if initiative.thread_id is None:
        ops.append(Op("create_thread", {
            "initiative_id": initiative.id, "title": title, "body": body, "tags": tags,
        }))
    else:
        new_hash = current_publish_hash(initiative, snapshot, cfg)
        if new_hash != initiative.published_hash:
            ops.append(Op("edit_post", {
                "initiative_id": initiative.id, "title": title, "body": body, "tags": tags,
            }))

    return ops


def _digest_op(digest: Initiative | None, snapshot: BoardSnapshot, cfg: MirrorConfig, now: int) -> Op | None:
    if digest is None or not digest.members:
        return None
    demoted_roots = [snapshot.cards[m] for m in digest.members if m in snapshot.cards]
    done_this_week = 0
    for card in demoted_roots:
        if not is_terminal(card.status):
            continue
        try:
            completed_at = int(card.completed_at) if card.completed_at is not None else None
        except (TypeError, ValueError):
            completed_at = None
        if completed_at is not None and (now - completed_at) <= _SEVEN_DAYS:
            done_this_week += 1
    body = render_digest(demoted_roots, snapshot, done_this_week, cfg.max_post_chars)
    title = cfg.digest_title
    new_hash = _digest_hash(title, body)
    if new_hash == digest.published_hash:
        return None
    return Op("ensure_digest", {"title": title, "body": body})


def plan(snapshot: BoardSnapshot, state: dict[str, Initiative],
         digest: Initiative | None, note_keys: set[str], cfg: MirrorConfig, now: int) -> list[Op]:
    ops: list[Op] = []

    curate_op = _unassigned_curate_op(snapshot, state, digest)
    if curate_op is not None:
        ops.append(curate_op)

    for initiative in sorted(state.values(), key=lambda i: i.id):
        if initiative.kind != "post" or initiative.archived_at is not None:
            continue
        ops.extend(_plan_initiative(initiative, snapshot, note_keys, cfg, now))

    digest_op = _digest_op(digest, snapshot, cfg, now)
    if digest_op is not None:
        ops.append(digest_op)

    return ops
