# Targeted independent review: first-batch CI correction

Review baseline: `127c25627673c4882d0edf83c146960c9ada62d2` (batch 2 local handoff). Review scope is the uncommitted test-only correction to `tests/cron/test_execution_ledger.py`; parent-authored decision/evidence documents and the eventual commit are outside this source-delta binding. No remaining feature work was reviewed or performed.

## Finding

No concrete P0/P1 blocker found. Fixture-only correction is justified. The original fixture patched `cron.executions.get_hermes_home`, but `_connect()` correctly resolves `cron.jobs._current_cron_store()` to keep the ledger and job store coordinated. That resolver gives an explicit `use_cron_store()` override priority and otherwise consults the real active profile home (subject to the existing deliberate module-constant compatibility override). A patched unused imported alias cannot model this contract. Production must retain the coordinated resolution rather than reverting it to make that obsolete patch effective.

The replacement uses `set_hermes_home_override()` and reset tokens around real ledger entrypoints. It retains exact row-list equality and distinct physical database assertions, uses equal job IDs across homes, and adds rejected cross-home mutation assertions. Nested profile exit restores the previous visible ledger. A second test exercises real `jobs.save_jobs/load_jobs` and ledger writes under explicit store overrides that differ from ambient home, including nested exceptional exit, exact row visibility, rejected cross-store mutation, and restoration to the ambient ledger. Independent SQLite reads confirm the selected and nested physical rows remain `claimed`; `closing()` closes those connections. No ledger/store resolver is mocked. The only setup patch restores the existing optional `EXECUTIONS_FILE` test override to its production value, `None`.

Production files `cron/executions.py`, `cron/jobs.py`, `cron/scheduler.py`, and `hermes_constants.py` were byte-compared with the baseline and are unchanged. The existing G17R registry test file is also baseline-identical. Inspection of applicable pytest fixtures found no conflicting cron-store override; actual hosted pytest integration still requires a fresh candidate run.

## Independent validation

Command executed from the source worktree:

```text
python3 ../run-approved-focused.py ../approved-ci-correction-review-tests.json tests/cron/test_execution_ledger.py tests/cron/test_v021_profile_registry.py
```

The task runner uses the canonical interpreter read-only with `-B -m unittest discover`, one fresh temporary HOME/HERMES_HOME/HERMES_BUNDLES_DIR/TMPDIR per file, source PYTHONPATH, and a sanitized subprocess environment. Results: **2 corrected/new ledger class tests passed; 8 existing G17R registry tests passed; 10 total**. Raw commands, exit codes, and output are in `../approved-ci-correction-review-tests.json`. `git diff --check` passed during review. No pytest installation, full suite, live operations, pushes, or staging changes occurred.

Important limit: unittest discovers the two `unittest.TestCase` methods in the ledger file, **not its 19 remaining pytest-style functions**. This is not a local full-file pytest or hosted-CI success claim. The observed SQLite WAL compatibility fallback warning was left unchanged; no runtime repair/install was attempted.

## CI status and immutable source evidence

Read `../CI-4cef65c-CORRECTION.md` and raw `../ci-failure-4cef65c-slice7.txt` lines 680–713. The raw slice records one failing case, `tests/cron/test_execution_ledger.py::test_execution_ledger_follows_the_current_profile_home`, with 19 other file cases passing. The user reports hosted CI `34545497788` **completed with failure**, with this case plus the aggregate failure. This review independently inspected the named raw slice, not every hosted job. **Fresh full CI bound to the new candidate is required**; prior queued-status receipts remain historical and must not be represented as final green.

SHA256 binding for reviewed corrected test:

```text
5c9cd645b2ac5f96b79c4f8277ca950bfba839debcaeb4962688a13816bdf619  tests/cron/test_execution_ledger.py
```

Supporting inspected source/receipt SHA256 values:

```text
62bd6671a0a81444b2b630422e216e947da2fe7577c5d3b0fec03df8ea3d24f6  cron/executions.py
68df05d7f5cec70657afbce78d22d507cdb7e8c407662a37934e6eb4fd300cdb  cron/jobs.py
ef20a9744462e06a3929f24380649bb6754fcc5121a719f5dc7a3772289a2254  tests/cron/test_v021_profile_registry.py
4a3ccd44fbf6529e48cd4648dca08ec1b80b4f707444e406df4da1d2c9557b89  ../ci-failure-4cef65c-slice7.txt
```

Independent reviewer wrote only this new task-local report and its new raw test receipt. All reviewer commands/test subprocesses completed, no agents were spawned, and no reviewer-owned child process remains. No source changes or previous-receipt edits were made by the reviewer.
