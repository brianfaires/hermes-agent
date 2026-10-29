"""Fork-only: org rules for dispatched Kanban workers that neither ``~/.hermes/ORG.md`` nor the protocol carry.

``agent/prompt_builder.py`` appends this to ``KANBAN_GUIDANCE``, so it reaches dispatcher-owned workers only. Its own
module keeps upstream's frequent edits to the protocol text from conflicting with it. Pure data: imported while
``prompt_builder`` is still loading.
"""

KANBAN_ORG_GUIDANCE = (
    "## Org rules\n\n"
    "- Pass `reviewer=\"ang\"` whenever you request review: only Ang has `sdlc-review`, and the dispatcher blocks a "
    "review assigned anywhere else."
)
