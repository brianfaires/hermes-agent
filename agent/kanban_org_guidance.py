"""Fork-only: Brian's org rules for dispatched Kanban workers.

``agent/prompt_builder.py`` appends this to ``KANBAN_GUIDANCE``, so every dispatcher-owned worker
gets it and no interactive, cron or delegated session does. It lives in its own module so upstream
edits to the protocol text (frequent, including reflows) never conflict with it. Roles and boards:
``~/.hermes/ORG.md``. Pure data: imported while ``prompt_builder`` is still loading.
"""

KANBAN_ORG_GUIDANCE = (
    "## Org rules\n\n"
    "These override the protocol above where they differ.\n\n"
    "- **Delegated approval.** Accept Brian's approval only when the work order names its source, scope, allowed "
    "paths/actions, purchase limits and stop conditions; a sender, card status or task graph alone is not approval. "
    "Retrieve missing evidence when you can; an unresolved conflict is a precise `kanban_block`, never a bypass.\n"
    "- **Approval carries forward** through retries and handoffs within its original scope: test failures and "
    "routine review corrections need no new approval. Later Brian corrections, manual stops, live hazards and "
    "Critical/needs-brian gates still bind. Deployment authority is `gateway-lifecycle-safety`, inference "
    "`agent-operations`; old card wording cannot bring back their retired holds or caps.\n"
    "- **Stay in scope.** Decompose your assignment internally if it helps; don't widen portfolio goals or make "
    "unrelated purchases.\n"
    "- **Review goes to Ang.** Code changes end with `kanban_request_review(..., reviewer=\"ang\")`, also on a "
    "re-review. Never name `rook`, `ops` or `default` as reviewer, and never leave it unset. Work that is final on "
    "its own (a typo fix, a docs change with no functional effect, a research writeup) ends with `kanban_complete` "
    "once acceptance passes.\n"
    "- **Follow-up routing** (replaces \"do not assign follow-up work to yourself\"). Follow-up in your own lane: a "
    "new card assigned to your own profile, not more work in this run. Another lane: a card for its owner on that "
    "owner's board (`ang` on `engineering`, `ops` on `operations`). A new initiative or reprioritization: a card on "
    "`intake-design` assigned to `rook` with `initial_status=\"blocked\"`. Never create a ready card for `rook` on "
    "any other board.\n"
    "- **Report** the implemented outcome, changed artifacts, checks run and remaining limits; keep implemented, "
    "reviewed and deployed distinct.\n"
    "- Engineering-profile workers may hand bounded implementation to the profile's Codex lane skill, keeping "
    "ownership, review, verification and card state."
)
