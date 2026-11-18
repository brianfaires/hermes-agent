# Scheduled evidence retention — preparation checkpoint

## Ask
Prepare the approved native retention correction for operations/t_99acfd62, the sole execution continuation of t_600d6ff7. Preserve newest 1,000 terminal rows plus latest valid scheduled evidence per job/source/status. Source preparation only; no deployment or restart.

## Plan
- [x] Verify canonical native admission and preserved worktree identity.
- [x] Qualify existing no-bootstrap interpreter and baseline ledger suite.
- [ ] Establish the regression RED in the approved coding harness.
- [ ] Implement retention and the remaining focused acceptance tests/docs.
- [ ] Verify rollback, native fixture, independent review, publication and applicable CI.

## State
As of 2026-11-18: **blocked, unverified WIP; not a source candidate.** Only an initial regression test is added. No production code changed. Parent baseline ledger suite passed (24 tests); Codex workspace-write runner then failed before collecting the new test with `OSError(30, 'Read-only file system')`. No behavioral RED/GREEN established. Runner source selects `/var/tmp/hermes-pytest-<uid>` rather than the supplied TMPDIR; this is a likely sandbox-path conflict, not a proven product failure. No sandbox bypass, bootstrap, provisioning, runtime repair or rerun attempted after the denial. Full evidence is attached to operations/t_99acfd62.

## Next
Controller must qualify an approved no-bootstrap sandbox-compatible test execution route before resuming this same continuation. Keep t_600d6ff7 held as acceptance wrapper; do not rerun its original launcher. Preserve this worktree and its original 13 ignored bootstrap receipts. This checkpoint is intentionally unpushed and must not be integrated or treated as verified implementation.
