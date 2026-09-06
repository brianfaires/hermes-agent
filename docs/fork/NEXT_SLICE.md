# Reconstruction continuation after inventory + FC-05

Task lineage: recovery `t_99b38c75` (sole writer after `t_5d084747` handoff).
Worktree: `/home/brian/.hermes/hermes-agent/.worktrees/t_99b38c75`
Branch: `brian/reconstruct-v0.21.0-recovery`

## Milestone status

- Inventory accepted; FC-03/FC-02/FC-07 previously migrated.
- Peer-audit ledger corrections applied (FC-13/06/25 false DROP_UPSTREAM risk).
- **FC-05** migrated: Hindsight provider toolset independent of file-memory.
- Codex local task quota remains exhausted until **2026-09-07 17:45**; continue Hermes-native.
- No main/staging/canonical writes, deploy, restart, or live config/DB/venv mutation.

## Immediate non-Critical migration order

1. **FC-03 / FC-02 / FC-07 / FC-05** — DONE.
2. **FC-13** — reproduce `prompt_path` on current cron tool/jobs/API (high value; was false DROP).
3. **FC-01 / FC-40** — reproduce value before any implementation.
4. **FC-25** — voice compression reproduction (deferred; Discord voice value).
5. **FC-16 / FC-22 / FC-28B / FC-36 / FC-37** — Critical or product gates; freeze design only until Brian approval.
6. **FC-11 / FC-41–FC-44** — Brian product/privacy/lifecycle only.

Accepted decomposition-guard repair `83fa9a9184658b6df9bbad9a77a8b70ddb3c8966` remains supplemental; activation stays separate controller lane.

## Boundaries

- Old `t_6d9a0f05` graph remains incident-held reference-only.
- Branch/tag pushes only to `brianfaires/hermes-agent` reconstruction refs.
- Preserve dirty state on timeout; do not reset this worktree from an old baseline.
