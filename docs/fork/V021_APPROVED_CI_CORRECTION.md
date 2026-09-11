# Approved narrow CI correction

First approved candidate `4cef65cada7ac1e483d117949862784beb8dc7aa`, hosted run
**34545497788**, completed **FAILURE**: exactly one test case plus aggregate failed,
per Ang's completed-run report. Raw slice7 job103097319943 confirms 19 passing/one
failing case in `tests/cron/test_execution_ledger.py`, lines680-713. Raw receipt
SHA256 and excerpt are in V021_APPROVED_CI_CORRECTION_TESTS.json. This supersedes
the queued status in immutable prior checkpoints; it is not a green CI claim.

Base: `127c25627673c4882d0edf83c146960c9ada62d2` (accepted-source batch2 handoff).
Only tests/cron/test_execution_ledger.py changes executable content. All production
blobs, including batch1 coordinated store alignment and batch2 isolation, remain
byte-identical. No feature work, runtime repair or assertion relaxation.

## Cause and correction contract

The old test patched `cron.executions.get_hermes_home`. G17R `_connect` now delegates
path selection to `cron.jobs._current_cron_store`, which honors explicit
`use_cron_store` first, compatibility constants second, then current profile home.
The patched alias was unused; both writes correctly reached the same actual store.
Changing production to consult that mock would split the execution ledger from
explicitly selected job stores. Real-path checks confirm a fixture correction
suffices; production behavior is retained.

The old test moves into a unittest.TestCase in the same pytest-collected file, so
it can execute locally without installing pytest. It uses real home ContextVars,
real create/list/transition entrypoints and physical temporary SQLite stores.
Exact per-profile list equality and both file-existence assertions remain; equal
job IDs now add cross-profile running/finish refusal and unchanged-row assertions.
A second test deliberately disagrees ambient home with nested explicit cron store
homes, writes real jobs and execution rows, verifies co-location, then throws an
exception to prove nested-store restoration. Independent physical DB reads check
that forbidden foreign transitions left selected/nested rows claimed. All home/
store tokens, temporary roots, override patches and physical DB readers are closed
or reset. Existing 19 other pytest functions are untouched.

## Focused qualification and review

Parent command:

```
python3 ../run-approved-focused.py ../approved-ci-correction-tests.json tests/cron/test_execution_ledger.py tests/cron/test_v021_profile_registry.py
```

Two corrected/added ledger tests and eight G17R regression tests passed. Parent
receipt precedes a closing-only resource cleanup; final independent review reran
the final file: all 10 tests passed with no P0/P1 blocker. The unchanged cumulative
checklist validator also passed (11 unique focused tests including validation). Canonical interpreter read-only, sanitized environment and fresh
task-local HOME/HERMES_HOME/HERMES_BUNDLES_DIR/TMPDIR; source PYTHONPATH, bytecode off.
Known safe SQLite DELETE-journal fallback warning observed, no repair performed.
Local unittest does not run the other 19 pytest functions; no claim of a full
module/full-suite pass. Hosted pytest collection/full CI remains required for the
exact newly integrated candidate. No live provider, scheduler, delivery or network
operation was needed. Review receipt is V021_APPROVED_CI_CORRECTION_REVIEW.md.

Ledger historical row/subfeature fields and prior receipts are preserved. Counts
stay 51 audit rows (37 old_main/14 archive_only),239 evidence objects,241 subfeatures,
52 landed checklist IDs and26 approved pending entries plus G32. Human UAT pending.

Ang independently verifies and integrates **batch2 plus correction**, then runs
fresh exact-SHA hosted CI. This bounded turn stops quiescent; no remaining feature
work started. No staging/main/canonical changes, push, install, activation/restart,
live config/credential/data/venv or other excluded operations. Final commit/tree,
clean status and targeted review binding are recorded in the external handoff
`../approved-ci-correction-evidence.md` after the local commit.
