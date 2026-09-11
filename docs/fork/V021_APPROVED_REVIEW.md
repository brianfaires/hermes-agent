# Independent native blocker review — G17R

Initial candidate: working-tree source/test delta against c0a3098235b47d88090966e38837953dc95ddf30. Review scope is G17R only: profile-qualified active claims, captured cleanup, local execution-ledger reconciliation, gateway global drain counts, interruption execution tokens and persisted owner fences. Ledger/checklist edits are excluded from the source candidate.

## Initial finding: P1 — remaining cross-profile liveness guard

`cron/jobs.py:257` uses the compatibility global raw-ID snapshot in `_job_running_in_this_process`. A live job `same` in profile A therefore reports profile B's distinct `same` as live. Both recurring stale-error recovery (`_job_is_stale_error_recurring`, line 1328) and exhausted finite one-shot recovery (`get_due_jobs`, line 4229) then suppress recovery for B. The registry change is incomplete at this sibling consumer. Use `current_profile_only=True` and add a real-store recovery regression preserving A's live job while B recovers. Parent accepted this finding; correction/re-review pending.

## Verified mechanisms

- Registration and since/future bookkeeping use the same resolved store/home key under one lock. Equal IDs across profiles can register independently.
- Executor cleanup captures home before submitting; its finally runs after copied context exits and releases the captured profile. Manual cleanup captures the same identity on success/failure paths.
- Stale sweep filters claims before ledger lookup and again before release. Live futures remain protected, terminal rows must postdate claim registration, and finite-repeat age recovery retains its existing guard.
- Execution ledger now follows explicit cron store override, keeping explicit profile B ledger lookup separate from runtime/default profile A.
- Gateway drain and scale-to-zero counts use qualified keys, avoiding raw-ID deduplication.
- Shutdown captures profile/owner/token and applies durable updates with expected_fire_owner; ownerless flags are qualified and next registration clears the old qualified flag.
- Migrated existing tests were reviewed for raw-key injection, consumer mocks, and changed release signatures. No additional concrete blocker found. Existing pytest tests were not run because the approved interpreter has no pytest; full hosted CI remains authoritative.

## Execution evidence

Command: `python3 ../run-approved-focused.py ../review-g17r-tests.json tests/cron/test_v021_profile_registry.py`

Result: 6 tests passed. The runner uses the canonical interpreter read-only, fresh isolated HOME/HERMES_HOME/HERMES_BUNDLES_DIR/TMPDIR, this source PYTHONPATH, and no inherited task scope. External scheduler maintenance/transport/process cleanup effects are faked by the tests. No installation, live state mutation, process-killer command, push or integration occurred.

Tracked source/test diff SHA-256: `13e9bc781ef7a580edae058bb92fea71ea8c7cc464eea9bd28ce3aec49dfbcb1`. New test is included in the per-file binding below (git diff does not contain untracked files).

```json
{
  "cron/executions.py": "62bd6671a0a81444b2b630422e216e947da2fe7577c5d3b0fec03df8ea3d24f6",
  "cron/scheduler.py": "e254674cbad613c17162be997a2cf18e319730c6e3b4b086e8e53034192a1b72",
  "gateway/run.py": "ea590642cf81001d24920efe91389b7c6e717fea5fcb70efe61ba3645d9b00e9",
  "tests/cron/test_cleanup_timeout.py": "3d190b27f0c4b86dd8bae6047f90a8e415c34cb63d3ae89872ba2a4e8f1b562f",
  "tests/cron/test_inflight_stale_guard.py": "f86bd4bd0eac0ce126730a6b5e8dd60532f9d2b50bfec83621b68b7e54194eaa",
  "tests/cron/test_parallel_pool.py": "24a5e2984d8c91bb63e99e247ec71f276d588c9cfa8efba3c533cc35bdac09c7",
  "tests/cron/test_recurring_wedge_selfheal.py": "973c08880f73cad202257f7ae9c53cacc2cbebaa94aae80538ce6d827721b414",
  "tests/cron/test_sessiondb_init_hang.py": "d61d9853f6993742929869b11438c9ed2a8c5155b7fcacdbd0b1df829cd97ad5",
  "tests/cron/test_shutdown_interrupt.py": "cb911194fcc9a16f457049045ae7ff7c82f6c69fa34d32fa3793db9b8b81df49",
  "tests/gateway/test_api_server_active_work_drain.py": "da2e7f98f88297a2c7de704d8b10c66fee0a9f5b4eea066da648fc99610e199a",
  "tests/gateway/test_cron_active_work_drain.py": "fdde26819577521388a167114f2a89dd2ea3efb0be6672665689b97d86d06ed2",
  "tests/gateway/test_cron_drain_floor.py": "89f02bbe84cfd4912c293febd883d3bb3e409757cd6bc4ea5649a275dee7c217",
  "tests/gateway/test_cron_interrupt_notification.py": "4440378edae3ce5ff111a69b1df5bb39d3b0ccbb2ca3868c715ae7812525f1b1",
  "tests/gateway/test_scale_to_zero_watcher.py": "abdafc993d6d52b63bd26caea12749cff2870364db2671715636843205bfab45",
  "tests/gateway/test_update_cron_drain.py": "cd4f76f17d6510a9e3f43dd84a59d0e3b6e759082e4dc44b07ba6b351e6f949b",
  "tools/cronjob_tools.py": "84d9a17ad71728785dccbcfe9d0e6390bfed904b78d9b7aa0b5e3215c1f545dc",
  "tests/cron/test_v021_profile_registry.py": "95dc37bf7ea7aea2e19eba9eb8c71d6ad4eb3714c3e576f0d6501a2061c0d31f"
}
```

## Targeted correction review — FINAL G17R receipt

The initial P1 is resolved. `cron/jobs.py:257` now explicitly filters the running snapshot to the current store. Two additional real `get_due_jobs` regressions demonstrate recurring stale-error recovery and exhausted one-shot retirement in B while A's equal-ID live job stays protected. The monitoring running-job metric and release-switch drain observation now count `get_running_job_keys`; these are narrow getter substitutions preserving existing exception behavior. Relevant imports/callers and test mock references were inspected. No additional P0/P1 acceptance blocker was found in the reviewed G17R delta.

Independent rerun: `python3 ../run-approved-focused.py ../review-g17r-tests.json tests/cron/test_v021_profile_registry.py` — **8 passed**, 1.668 seconds test runtime, return code 0. Receipt JSON now contains this final rerun. The SQLite warning is existing safe journal fallback behavior and was not repaired or changed. No pytest/full suite claim is made. Human UAT remains deferred, and remaining restoration gates are outside this receipt.

Source and G17R tests were quiescent for this final receipt. Parent may bind these exact hashes to the subsequent commit; documentation-only updates do not change this source candidate. No child processes remain from this review.

Final tracked source/test diff SHA-256: `6943eac940421fb1ee8cf5cb9d42110b0a129aa8f171c810dedcf290bccf6646`. Final per-file binding (including new untracked regression test):

```json
{
  "agent/monitoring/cron_health.py": "cecf159087d7b9c578499f711a98a36cf5c19869615543e948f80439fbe166fa",
  "cron/executions.py": "62bd6671a0a81444b2b630422e216e947da2fe7577c5d3b0fec03df8ea3d24f6",
  "cron/jobs.py": "68df05d7f5cec70657afbce78d22d507cdb7e8c407662a37934e6eb4fd300cdb",
  "cron/scheduler.py": "e254674cbad613c17162be997a2cf18e319730c6e3b4b086e8e53034192a1b72",
  "gateway/run.py": "ea590642cf81001d24920efe91389b7c6e717fea5fcb70efe61ba3645d9b00e9",
  "scripts/claude_release_switch/runtime_observation.py": "bbec33e29f70ca9c4fed2a3c2324c6b4a073df4a5d1f70c43fe44a5fbed166c7",
  "tests/cron/test_cleanup_timeout.py": "3d190b27f0c4b86dd8bae6047f90a8e415c34cb63d3ae89872ba2a4e8f1b562f",
  "tests/cron/test_inflight_stale_guard.py": "f86bd4bd0eac0ce126730a6b5e8dd60532f9d2b50bfec83621b68b7e54194eaa",
  "tests/cron/test_parallel_pool.py": "24a5e2984d8c91bb63e99e247ec71f276d588c9cfa8efba3c533cc35bdac09c7",
  "tests/cron/test_recurring_wedge_selfheal.py": "973c08880f73cad202257f7ae9c53cacc2cbebaa94aae80538ce6d827721b414",
  "tests/cron/test_sessiondb_init_hang.py": "d61d9853f6993742929869b11438c9ed2a8c5155b7fcacdbd0b1df829cd97ad5",
  "tests/cron/test_shutdown_interrupt.py": "cb911194fcc9a16f457049045ae7ff7c82f6c69fa34d32fa3793db9b8b81df49",
  "tests/gateway/test_api_server_active_work_drain.py": "da2e7f98f88297a2c7de704d8b10c66fee0a9f5b4eea066da648fc99610e199a",
  "tests/gateway/test_cron_active_work_drain.py": "fdde26819577521388a167114f2a89dd2ea3efb0be6672665689b97d86d06ed2",
  "tests/gateway/test_cron_drain_floor.py": "89f02bbe84cfd4912c293febd883d3bb3e409757cd6bc4ea5649a275dee7c217",
  "tests/gateway/test_cron_interrupt_notification.py": "4440378edae3ce5ff111a69b1df5bb39d3b0ccbb2ca3868c715ae7812525f1b1",
  "tests/gateway/test_scale_to_zero_watcher.py": "abdafc993d6d52b63bd26caea12749cff2870364db2671715636843205bfab45",
  "tests/gateway/test_update_cron_drain.py": "cd4f76f17d6510a9e3f43dd84a59d0e3b6e759082e4dc44b07ba6b351e6f949b",
  "tools/cronjob_tools.py": "84d9a17ad71728785dccbcfe9d0e6390bfed904b78d9b7aa0b5e3215c1f545dc",
  "tests/cron/test_v021_profile_registry.py": "ef20a9744462e06a3929f24380649bb6754fcc5121a719f5dc7a3772289a2254"
}
```

## Independent G17 channel-policy receipt — FC-17-E08/E09

Baseline: `d97ba560254c93c14704b13b64fbdd5bb31ecd6a`. Bounded review covers only the Discord adapter channel-precedence delta and new `tests/gateway/test_v021_discord_channel_precedence.py`, against `../g17-channel-contract.md`. Previous G17R receipt remains separate above. No other restoration slice is covered by this receipt.

**No concrete P0/P1 acceptance blocker found.** The new preference is explicitly enabled only by allowed_channels and ignored_channels accessors. Explicit extra key presence retains empty-list and empty-string meaning before environment fallback. A nonempty captured profile snapshot remains first priority. An empty captured value remains authoritative against process environment because `_gate_env` returns its default for present snapshot keys; the new path does not introduce a direct environment read. Existing user/role/no-thread gate callers retain default legacy precedence. Deny handling and resolved real-channel/thread checks downstream of accessors remain unchanged.

The real loader test exercises top-level, platforms, and gateway.platforms configuration with populated and empty channel lists, confirms the plugin-owned bridge supplies the adapter values, and asserts no changes to the two process channel variables. Tests also exercise actual `send` denial with fake channel transport, connected snapshot deny priority, an empty multiplex secret scope that cannot borrow process environment, and standalone explicit-empty policy without network lookup. No core bridge duplication was added. The supplied red receipt documents pre-fix failures; this reviewer did not mutate/revert source to reproduce the red state.

Independent command: `python3 ../run-approved-focused.py ../review-g17-channel-tests.json tests/gateway/test_v021_discord_channel_precedence.py` — **7 passed**, 10.718 seconds test runtime, return code 0. Canonical interpreter used read-only through the approved isolated runner. No package installation, connection/send to real Discord, live configuration write, voice work, push/integration, or process-killer action. Tests use faked network/transport where exercised. Hosted full pytest and deferred human UAT are not claimed by this receipt. Review process has exited; source/test candidate was quiescent.

Tracked scoped diff SHA-256: `16bd026c4b989af7c77c29c4002516e1764796cfe8fac4ff4c7ee79558a4eec0`. The new untracked test is bound by the per-file hashes below:

```json
{
  "plugins/platforms/discord/adapter.py": "891de01c723e6174487dcb70aafaf6911e079b1023282c5a6cb8b69e300cf04c",
  "tests/gateway/test_v021_discord_channel_precedence.py": "7b5ef2aea6a2ef41100ee57a622edd4dba7becfeed62972efbd0b3602b252cc7"
}
```

## Parent commit binding

G17R source: `d97ba560254c93c14704b13b64fbdd5bb31ecd6a`.
G17 channel source: `aba1042c5c72c58c3e33f9b8a979405f8069f82e`.
These commits contain the exact source/test hashes in the independent receipts.
