# Scheduled evidence retention — source preparation

## Ask
Prepare the approved native retention correction for operations/t_99acfd62, the sole execution continuation of t_600d6ff7. Preserve newest 1,000 terminal rows plus latest valid scheduled evidence per job/source/status. Source preparation only; no deployment or restart.

## Plan
- [x] Verify canonical native admission and preserved worktree identity.
- [x] Qualify existing no-bootstrap interpreter and baseline ledger suite.
- [x] Establish the regression RED in the approved coding harness.
- [x] Implement retention and focused acceptance tests/docs.
- [x] Focused retention/recovery coverage: 107 passed; additional `test_cron_recovery.py` timeout reproduced with the exact old production module.
- [x] Native isolated fixture and rollback proof produced; Ops consumer compatibility remains separately owned.
- [ ] Parent: frozen checkpoint, independent review and source-branch publication.
- [ ] Separately authorized integration/release: exact-head CI, deployment and natural acceptance.

## State
As of 2026-11-18: resumed on the existing clean `wt/t_600d6ff7` at
`27e24f97a7ed7956f39c94a2b32f3baaea217acf`, including the parent's reviewed runner
scratch-root correction. The preserved regression demonstrated genuine RED:
1,001 unrelated native completions pruned the weekly scheduled execution. The
same regression passed after the retention-only correction. The expanded
acceptance tests and ten focused files passed (107 tests), including old-snapshot
due/claim suppression, native recovery and delivery settlement. The overall
eleven-file run failed because `test_cron_recovery.py` exceeded 300 seconds.
Collection completes, but its isolated receipt-settlement test also times out
inside `asyncio.run(probe())`; the thread dump does not establish a root cause.
A controlled exact-base `cron/executions.py` substitution reproduced the same
selected-test timeout under the same runner; the candidate was restored byte-for-byte.
That additional integration test remains unqualified, but retention did not introduce
its timeout. Its repair is outside this bounded change. Native fixture generation
and old-policy rollback both passed: rollback can discard extra scheduled proof.
Independent review and source publication are next; no deployment is claimed.

The original sandbox failure and artifacts remain historical evidence. Current
invocations use the parent-qualified existing interpreter, canonical runner,
`env -i`, isolated task HOME/TMPDIR, explicit task scratch root, two workers and
zero file retries. No provisioning, escalation or alternate runner is used.

## Behavior and tradeoff

Keep the original newest-1,000 terminal window and the latest valid scheduled row
per `(job_id, source, status)`. Native aware occurrence time chooses the pin;
the original terminal/claim-time and ID order resolves ties. Explicit manual
source, null/invalid/naive occurrences and UTC normalization overflow cannot pin.
Active rows, schema and lifecycle writers stay unchanged. Fixed-arity deletes
avoid a SQL parameter list that grows with identity count.

The extra bound is five terminal rows per historical job/source pair, overlapping
the recent window where applicable. Identity churn can grow storage and pruning
work; this is intentionally not a fixed database-size ceiling. Retained completed
slots suppress old-snapshot replay beyond the global window. Different later
slots and explicit manual runs remain eligible. Queued delivery still settles
independently; verified recovery receipts retain their existing semantics.

The user guide's execution-history contract and developer cron-internals
occurrence/provider/delivery contracts were consulted. Every native terminal
writer uses the same pruning function; no supported provider extension or
retention setting supplies this invariant. A larger fixed cap would still lose
sparse evidence under unrelated traffic. No new store, producer or backfill is
introduced.

## Next
Fresh logs, exact commands, exits, counts and hashes are recorded in
`/home/brian/.hermes/kanban/boards/operations/workspaces/t_99acfd62/resume-implementation-evidence.md`
and sibling `resume-*.log` artifacts. Prior attempt artifacts are untouched.
`verification-closeout.md` and sibling artifacts record the old-source timeout
contrast, exact restoration, native fixture and rollback checks. Parent owns
frozen review and source publication. Source preparation is not deployed prevention
evidence; no live job replay or restart is authorized.
