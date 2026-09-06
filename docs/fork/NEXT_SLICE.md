# Reconstruction continuation after inventory milestone

Task lineage: recovery `t_99b38c75` → continuation `t_5d084747`.
Worktree: `/home/brian/.hermes/hermes-agent/.worktrees/t_99b38c75`
Branch: `brian/reconstruct-v0.21.0-recovery`
Inventory baseline HEAD before this milestone commit: `495b851fbc0b8b3531e56db5373b4bf4446ff288`.

## Milestone status

- Map-only disposition/omission/UAT ledgers accepted after mechanical coverage + native second-pass semantic review.
- Codex local task quota remains exhausted until **2026-09-07 17:45**; CLI 0.136.0 also fails models-catalog decode on effort variant `max`. Do not idle reconstruction on Codex. Prefer Hermes-native engineering; deferred Codex review is optional when quota returns.
- No main/staging/canonical writes, deploy, restart, or live config/DB/venv mutation from this lane.

## Immediate non-Critical migration order

1. **FC-03** — IPv4-only/unavailable loopback family in `find_free_debug_port` (live RED on this host).
2. **FC-02** — installer/doctor refuse global launcher repair from a linked Git worktree.
3. **FC-07** — Langfuse multiline path-like payload neutralization (plugin-local; privacy/observability review, no enablement change).
4. **FC-01 / FC-40** — reproduce value before any implementation.
5. **FC-16 / FC-22 / FC-28B / FC-36 / FC-37** — Critical or product gates; freeze design only until Brian approval.
6. **FC-11 / FC-41–FC-44** — Brian product/privacy/lifecycle only; no automatic cards.

Accepted decomposition-guard repair `83fa9a9184658b6df9bbad9a77a8b70ddb3c8966` is supplemental to the 157-commit legacy union; assess proportionally for v0.21, never blind cherry-pick. Live activation stays on the separate controller lane.

## Boundaries

- Old `t_6d9a0f05` graph and descendants remain incident-held reference-only.
- Branch/tag pushes only to `brianfaires/hermes-agent` reconstruction refs.
- Preserve dirty state on timeout; do not reset this worktree from an old baseline.
