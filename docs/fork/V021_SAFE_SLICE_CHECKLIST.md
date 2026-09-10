# Cumulative safe-slice checklist — human UAT deferred

Continuation test/source candidate: `76d908a34804889c9c27cebd08619e71528070e4`.
Final documentation-bound HEAD is in `../continuation-result.md`. Previous
exact `3c8f7d0` staging CI **passed**, run 34523897060, including 52 added tests.
The prior pending-CI and partial-milestone language below is historical.

Brian is unavailable for human testing. Keep all separable safe development
moving; human checks accumulate and are not development prerequisites. Existing
[Critical gates](critical-gates.md) remain unchanged. No activation, main
promotion, live restart/install/send/config/data/credential change is claimed.

## Ang's next source-only integration checks

1. Inspect local continuation commits and the native blocker review in
   `../continuation-review.md`; verify exact covered candidate.
2. Rerun the six new test files listed in
   [continuation qualification](V021_CONTINUATION_QUALIFICATION.md), using the
   canonical interpreter read-only and isolated task environment. Expected:
   seven tests pass; all filesystem writes remain in disposable fixtures.
3. Integrate/push to source-only staging, then verify fresh exact-SHA hosted CI,
   including actual collection of all six files. No new CI result is claimed.
4. Verify ledger preservation: 51 IDs, 37 old-main/14 archive-only, 239 original
   evidence entries, 241 split subfeatures, 46 unchanged Critical entries.

## Added future human checks (deferred)

- In a separately authorized disposable deployment, select a model for a named
  profile from a delayed picker; only that profile's settings should change.
- Confirm allowlisted secondary cron profiles show their own heartbeat and
  adapter identity; excluded profiles remain inactive.
- Confirm a platform connected only for a secondary profile can deliver that
  profile's task notification. G19 policy decisions must be resolved before
  testing changed destination behavior; this continuation changes none.
- Confirm shared restart preflight reports all active work and actual restart
  waits for caller delivery, drains and reconnects the intended profiles under
  its supervisor. Local tests used transport/process stubs, not a live restart.
- Confirm linked-worktree setup leaves the canonical launcher/shell unchanged;
  the automated fixture executed only extracted setup_path, never bootstrap.

The original human checks and rollback groups remain below as an explicitly
historical checklist, not current permission or a reason to stop development.

## Historical correction checklist (superseded policy/status)

# Restored safe slice — candidate-bound human checklist

Source candidate: `7abcbc9d82e4216da4242d75d599d43aafbb96fe`. Documentation is committed afterward; bind this checklist to the exact final git HEAD in the correction report. **Do not run or activate this candidate now.** This is a source review and future isolated-validation checklist, not whole-scope readiness. Root initiative remains blocked on [precise Critical decisions](critical-gates.md).

1. Confirm the final candidate descends from that source SHA and differs only by handoff documentation. Expected: exact source commit, clean status, all 51 audit IDs and 239 historical entries intact.
2. Ang pushes that exact candidate to source-only staging and inspects the fresh hosted CI. Expected: restart service-detection, drain, progress, e2e plaintext restart and TTS dotenv failures clear; all original 51 new tests plus the added no-SDK case collect. The earlier run is evidence only for e36f3d7.
3. Inspect the focused safe-slice receipts: cleanup preserves untracked descendants; strict cron prompts reject unsafe effective content without persistence; inventory ignores worker DB pins; children do not consume worker lifecycle. Expected: isolated tests pass with existing stronger refusals retained.
4. Inspect provider/display/delivery receipts: configured ElevenLabs settings reach both paths; defaults omit SDK settings; progress selects a meaningful bounded command; model and busy slash restart wait for caller delivery without bypassing audit/drain. Expected: no live synthesis, sends or restart in validation.
5. Inspect the compression/skills/process receipts: failed compression preserves bookkeeping; future skill builds refresh while conversation prompts remain frozen; timed-out transforms clean only their own test process tree. Expected: named isolated tests pass, no published-child rollback claim.
6. After fresh CI passes, request a **separate activation decision** with exact SHA, disposable test environment and rollback selection. Any actual human runtime validation must wait for that approval. Main promotion is also excluded from this batch.

Exclusions: canonical/main checkout, live state/configs/credentials/venvs, Kanban, all parked Discord voice work (FC24 and voice-specific FC17), archive-only features and intentional policy removals. General FC04 TTS and FC25 compression are the scoped exceptions already documented; they do not import the voice branch.

Rollback groups (planned only; no rollback performed):

- CI correction: revert `7abcbc9d82` as one cohesive source/test unit only if withdrawing the correction; it would restore the known CI failures.
- Restart: initial delivery barrier plus prepend callback and busy-inline delivery fixes form one group; keep authorization/audit and drain safeguards together.
- Strict cron scanner coverage: core create/update, tool/API and fire-time paths form one group.
- Worker lifecycle and inventory: use the distinct owning commits in the ledger, preserving baseline FC20 task/tool hotfix and durable mutation refusals.
- TTS/settings, compact display/Discord text, skill refresh, compression bookkeeping, cleanup/process handling: revert each ledger-owned slice with its tests and dependencies; consult cross-slice notes in the handoff.
- Dependency floors: manifest and lockfile together. Documentation/evidence commits are separately reversible and should remain as historical records.
