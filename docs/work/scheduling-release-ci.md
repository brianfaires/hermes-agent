# Scheduling release CI prerequisites

## Ask
Deliver the reviewed scheduling repair with current main preserved, exact-head strict hosted CI,
and ordinary qualified release. Issue251 / PR261 and operations/t_3f7d7068 remain the delivery path.
This round is a bounded prerequisite correction; parent owns checkpoint, review and release.

## Current state
Round 6, 2026-11-27 Pacific. Base/head: `998285bc1aeddaffdf5dc99856bd5d3c08d1add3`.
Strict run `46103449433` on that exact revision failed: 44 successful, 8 skipped, 5 failed jobs
(including the strict aggregate). Desktop core/reconnect and ARM64 suspension kernel proof now
pass hosted; endpoint/other holder tests also passed. Those implementations are unchanged here.

Four remaining failure scopes have fixture-only corrections prepared:

- Windows x64 Git: the canonical runner removes the setup PM store override and isolates the
  home. Bind the live test to the existing, recorded runner Git store; production discovery stays
  unchanged. Require a matching PM entry and retain real binary probes and all five cases.
- ARM64 holder: the surviving descendant is conhost.exe in C:\\Windows, while both Python cwd
  holders close and promotion succeeds. Spawn fixture helpers without a console; retain every
  captured-child exit check and every sibling survival/consent assertion.
- Pinned N-1 `2415b14ccf13a3d3cf58fc17e7765ddbae3298ad`: boot maintenance replaces the fixture's
  working legacy launcher with a PM launcher before a dependency generation exists. Use the
  predecessor's supported CLI exposure setting during legacy setup, then restore normal exposure
  immediately before the real updater. Reuse the fixture's provisioned dependencies; preserve all
  seven handoff properties, old/new imports, dashboard health and update assertions.
- macOS browse DDL: artificial SQL delay was active during macOS durability/schema setup before
  the deadline handler. Start the artificial delay and readiness signal at BEGIN IMMEDIATE,
  independently of the production handler. Keep the existing budget, ownership wait, elapsed
  bound, rollback, byte-preservation and recovery-message checks.

Root evidence and source dataflow were recorded before edits in `.cache/round6/ROOTS.md`.
Full report, static receipts and complete patch are retained in `.cache/round6/`.
No production code, scheduling blob, dependency, predecessor pin, timeout or CI gate changes.

## Validation and next step
Local behavioral execution remains unavailable: the canonical runner selects `/var/tmp`, outside
this lane's writable scope, and the prior namespace preflight denial must not be retried or bypassed.
No local RED/GREEN or native test pass is claimed. Static results are in the round report.

Parent must freeze the diff, obtain independent review, and run the four focused scopes on their
actual hosts, followed by exact-head `ci.yaml release=true` with successful
`All required checks pass (v2)`. Existing successes do not qualify this new candidate.
No commit, external publication, review, release or live scheduling retry occurred in this lane.
Full delivery and original retry acceptance remain owed.
