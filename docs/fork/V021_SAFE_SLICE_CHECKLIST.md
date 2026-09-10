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
