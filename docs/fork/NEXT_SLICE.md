# v0.21 release preparation — 2026-09-06

Worktree: `/home/brian/.hermes/hermes-agent/.worktrees/t_99b38c75`.
Branch: `brian/reconstruct-v0.21.0-recovery`.

Brian's 2026-09-06 approval explicitly supersedes the old freezes for FC-16,
FC-22, FC-28B and FC-36. Those four are implemented with executable RED/GREEN
qualification. Current staging's decomposition guards and fallback alerts are
ported surgically. Current main's FC-49 notification opt-in policy is restored;
the earlier `DROP_UPSTREAM` claim was incomplete.

The actual upstream ancestor is stable `v2026.8.31`, peeled
`29112bef099274229cadff79cdff7bf7b99c4b77` (package version `0.21.0`). The external
CONTROLLER.md statement that reconstruction is based on v0.19 is superseded by
this verified graph fact.

Local preparation is separate from release. The exact committed candidate,
independent blocker-review binding, fresh-materialization results, commands,
logs and parent-owned deployment route are recorded in
`/home/brian/.hermes/profiles/ang/profiles/ang/projects/hermes-v021-rebuild/evidence-20260906/handoff.json`.
Do not infer reviewed, CI-qualified, or live status from this document alone.

Next owner: Ang. Qualify the exact staging SHA in hosted CI, then perform the
supported drain/stop/update/start procedure only within the release window
(expiry `2026-09-06T21:20:29-07:00`) and with rollback time remaining. The older
live gateway has no v0.21 control socket; do not assume the new pause API is
available before upgrade or call a Git checkout byte-atomic.

FC-37 (Hindsight history privacy), FC-11, FC-42/43/44 remain deferred. FC-41
(gateway `/new (<prompt>)`) was resolved by Brian and now has an implemented,
reviewed-pending candidate that is NOT deployed. No new
privacy/auth policy or release controller is included. Old worktrees, refs and
the held `t_6d9a0f05` graph are preserved. No Kanban action, live mutation,
service restart, main/staging move or push was performed during preparation.
