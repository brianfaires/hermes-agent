"""Fork-only: Brian's org rules for dispatched Kanban workers.

``agent/prompt_builder.py`` appends this to ``KANBAN_GUIDANCE``, so every dispatcher-owned worker
gets it and no interactive, cron or delegated session does. It lives in its own module so upstream
edits to the protocol text (frequent, including reflows) never conflict with it. Roles and boards:
``~/.hermes/ORG.md``, which workers already load as a context file, so this carries only what ORG
and the protocol don't. Pure data: imported while ``prompt_builder`` is still loading.
"""

KANBAN_ORG_GUIDANCE = (
    "## Org rules\n\n"
    "- **Approval.** Brian's delegated approval counts only when the work order names its source, scope, allowed "
    "paths/actions, purchase limits and stop conditions. Within that scope it carries through retries, handoffs, "
    "test failures and routine review fixes; later Brian corrections, manual stops, live hazards and "
    "Critical/needs-brian gates still bind. Deployment authority is `gateway-lifecycle-safety`, inference "
    "`agent-operations`; old card wording cannot revive holds or caps they retired.\n"
    "- **Reviewer is `ang`.** When the protocol calls for same-card review, pass `reviewer=\"ang\"`, re-reviews "
    "included: Ang is the only profile with `sdlc-review`, so any other reviewer, or none (yourself), fails. "
    "Behaviour changes (code, scripts, config, skills, cron) get review; work final on its own (typo, docs with no "
    "functional effect, research writeup) completes once acceptance passes.\n"
    "- **Follow-up routing** (replaces \"do not assign follow-up work to yourself\"). Own lane: a new card for your "
    "own profile, not more work in this run. Another lane: a card for its owner (`ang` engineering, `ops` "
    "operations); `parents` resolve only on the same board, so a linked child stays on your board and standalone "
    "work goes on the owner's board citing your card id. New initiative or reprioritization: `intake-design`, "
    "assignee `rook`, `initial_status=\"blocked\"`; Rook takes it in its portfolio pass.\n"
    "- **Report** outcome, changed artifacts, checks run and limits; keep implemented, reviewed and deployed "
    "distinct."
)
