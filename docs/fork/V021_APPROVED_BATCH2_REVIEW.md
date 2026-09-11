# Independent native blocker review — batch 2

Status: complete for the frozen production/source candidate below; no concrete P0/P1 blocker found. This is a new receipt; batch 1 evidence/review were not modified.

Baseline: accepted `4cef65cada7ac1e483d117949862784beb8dc7aa`.
Reviewed initial commits: `43407c957e86052cb08995bc97dd55f294a5a3ec` (ownership), `38a634aee34952f3e66d7b2bceba95b438650526` (credentials).

No concrete P0/P1 acceptance blocker found in these two groups. Read their complete production diffs, tests, pre-mutation contracts and implementation receipts, plus approved continuation restrictions. Review is limited to new approved batch 2 delta; no completed-feature re-audit, voice, retired, archive-only, live, staging, install or push work.

Ownership: durable profile stamps govern exact and fallback recovery; legacy route fallback remains compatible. Explicit resume checks target and all rebound compression ancestors within BEGIN IMMEDIATE; outgoing promotion, reopen, reset-child stabilization, peer metadata and selected route publish in that transaction. Existing branch/delegate/tool lineage exclusions remain. Memory route publication follows durable success, mirror failure does not undo DB success, and generation ordering protects delayed snapshots.

Credentials: per-home private refresh is serialized by existing cache lock and returns its own snapshot. Scope token lifetimes restore on exceptions. Single-profile shell fallback remains; multiplex subprocess environment fails closed without scope and excludes unknown inherited values. Script scope resolves before existing sanitizer. Named bot receiver uses destination secrets and restores sender context. Late dotenv imports in scoped execution avoid process writes. Shared bounded executor remains and safe concurrent scripts qualify context isolation.

Independent command:

```
python3 ../run-approved-focused.py ../batch2-review-ownership-credentials-tests.json tests/gateway/test_v021_durable_ownership.py tests/cron/test_v021_private_credentials.py
```

Result: **21 tests passed** (12 ownership, 9 credentials). Real temporary SessionDB/SessionStore and SQL rollback trigger; real dotenv/config imports; two harmless real script children; external sources and bot delivery faked. Canonical interpreter read-only with clean isolated HOME/HERMES_HOME/HERMES_BUNDLES_DIR/TMPDIR, source PYTHONPATH and disabled bytecode. SQLite emitted its existing safe DELETE journal fallback warning; no repair attempted. Test runner exited and executor/script children joined.

Limitations: no live provider/secret-source/platform calls, hosted full CI or human UAT. Named bot test verifies passed child environment; custom mounted-root CLI startup depends on real profile resolution and is not independently E2E qualified by that fake transport test. Human acceptance remains pending.

## Frozen complete batch 2 source review

Candidate: `59b7ab83634cdf1e57c091e9072002b18b7eba3b`.
Candidate tree: `8379ce7036b86bb76c284d3a4a1dda8a01672207`.
Transport commit: `59b7ab83634cdf1e57c091e9072002b18b7eba3b`.
All 12 changed production/test files match their candidate blobs byte-for-byte at final inspection. Parent is separately maintaining ledger/checklist/evidence; this source review does not claim review of subsequent documentation commits.

Reviewed the complete transport production diff, pre-mutation contract, new tests and adjacent source paths. Launch identity is pinned independently of ambient routed ContextVars; runtime-home cache key is stable for a given home and prevents cross-home reuse. Explicit unresolved secondary runtimes raise rather than falling back. Adapter construction home owns Discord persistent state and pairing. Source owner is stamped at build time, existing route result still wins, and live transport provenance is not serialized. Stale/unregistered/stopped retained transport cannot fall through to another routed bot. Pairing store and DM policy use transport ownership; absent secondary pairing store fails closed. All six delayed nonvoice control views capture transport pairing home and gate values while retaining existing user/role/admin behavior. Runtime scope setup failure restores home even before secret-token creation.

Independent transport command:

```
python3 ../run-approved-focused.py ../batch2-review-transport-tests.json tests/gateway/test_v021_transport_identity.py
```

Result: **15 tests passed**. Real temporary pairing grants and adapter construction/state writes, actual authorization entrypoint, actual approval-send method with fake network send, all six view checks; every view stopped. Combined independent new-feature validation: **36 passed**. Also inspected parent's supplemental receipt `batch2-transport-tests.json` (7 channel-precedence and 2 existing profile-scope tests pass); these 9 are parent-run, not counted as independent reruns. `git diff --check` over baseline-to-candidate passed.

No production source changes were made by this reviewer. No child agents spawned. Both focused runner subprocesses exited successfully; safe script executor workers and view tasks were joined/stopped by tests. No installs, full local pytest, external network/platform operations, live/canonical/staging writes, process-killer calls or pushes. Prior receipts preserved. The full restoration initiative and human UAT remain pending; this review qualifies only this cohesive approved batch.

### Exact reviewed SHA256

```
66a98f7670e875b980f1a5de1211d0b71cfa272c0fa929c937d81a4dc0369224  agent/secret_scope.py
858ad47fd4b5a3466a96aa136b50348da3139b1dd49e4bbc8a3d6db165ceecdd  cron/scheduler.py
aeea69961e52c59a16a5aef7b87a0680949db37103f0d77ba5d133485e24720d  gateway/authz_mixin.py
d0da778b63081ee6484a0472324bcd1ddb86d1b3f4a6a86d84997456e13b4d0b  gateway/platforms/base.py
150c0ef1f3fb0eba651d38d80e75aec7b3bf4b16cac66f2fd99abb907ec12e70  gateway/run.py
467bcc4f6b6ebb9c53a950df00c83c32a1e700a50226268c209871130c80cf2f  gateway/session.py
2859447a0399ee2872ac10b566f734a213a46a93152e95120d251ecf7a00c4de  hermes_cli/env_loader.py
c6a66029ebb18c6b7621cf8725310d653a9a34006ba8c837e7e223455d1d5280  hermes_state.py
5923213d42d05e7740ac9f00caf1eba02ee2b27c913423dd1c361786cc5e468d  plugins/platforms/discord/adapter.py
48cc0f55743ba154f79f5f08f848e7c8c448c4ab636de5943f86360eb24f6118  tests/cron/test_v021_private_credentials.py
601645d70ef0b7aef5eda7f84f088a4a982b7b9c4e218f4127ec87ad4faf0d80  tests/gateway/test_v021_durable_ownership.py
364428e2cd722f3972aebeea2c8ffeb609b8563cb012eb20f232dd0a6f751493  tests/gateway/test_v021_transport_identity.py
```
