# Reconstruction continuation after FC-25

Task lineage: recovery `t_99b38c75` / continue card `t_21b82a07`.
Worktree: `/home/brian/.hermes/hermes-agent/.worktrees/t_99b38c75`
Branch: `brian/reconstruct-v0.21.0-recovery`

## Milestone status

- Inventory + FC-03/02/07/05/13/40 migrated earlier.
- **FC-01** reproduced and **DROP_LOW_VALUE** (no live multi-mutation consumer; current atomic set/unset sufficient).
- **FC-25** reproduced and **KEEP** (spoken-TTS preservation ported; atomic rotation already present).
- Codex local quota until **2026-09-07 17:45**; native path continued.
- No main/staging/deploy/runtime mutation.

## Immediate non-Critical order

1. FC-03/02/07/05/13/40/01/25 — DONE for executable non-Critical set on this card.
2. Remaining program: Critical/product freeze only (FC-16/22/28B/36/37 and FC-11/41–44) + candidate handoff.
3. Do **not** claim full rebuild complete.

## Boundaries

- Old `t_6d9a0f05` graph remains held.
- Branch/tag pushes only to `brianfaires/hermes-agent` reconstruction refs.
