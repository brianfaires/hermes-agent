# Feature disposition ledger — Hermes v0.21.0 reconstruction

Every local behavior must pass both gates:

1. **Need gate:** Is the behavior still useful enough to justify carrying and operating it?
2. **Placement gate:** Can it live outside upstream core?

Preferred placement order:

1. Existing upstream configuration
2. Skill or operational runbook
3. Standalone profile plugin under `~/.hermes/plugins`
4. In-tree plugin or platform adapter
5. Existing service-gated tool or hook
6. Small generic upstream seam plus external implementation
7. Direct upstream-core patch only when no safer boundary exists

Allowed decisions: `KEEP`, `REWRITE`, `DROP_UPSTREAM`, `DROP_LOW_VALUE`, `DROP_OUT_OF_SCOPE`, `DEFER_HUMAN_VALUE`, and `DEFER_REPRODUCTION`.

For each feature record: legacy commits/branches, current symptom, upstream evidence, value judgment, chosen placement, migration commit, tests, review, rollback tag, and UAT requirement.

No historical commit is replayed merely because it exists. Mixed commits are split by behavior; historical dependency pins are regenerated from the v0.21 dependency graph.

| Feature | Legacy source | Current symptom | Decision | Placement | Migration commit | Tests | Rollback |
|---|---|---|---|---|---|---|---|
| Run hosted CI on persistent staging pushes before main promotion | `597d7030585eb574e30b40eb44c11764f79f7891` | Current `.github/workflows/ci.yaml` only runs push orchestration on `main`, so staging pushes do not receive the complete hosted gate. | `KEEP` | Existing upstream CI orchestrator trigger | This slice | `tests/ci/test_staging_workflow_gate.py` | Revert this slice to remove the staging push trigger. |
| Disk-cleanup preserves durable scripts across profiles | Candidate `366a66d266306de0cb91c5fb9db14fceeea4a919`; related earlier disk-cleanup wildcard work `7a54c4f9c6c3562e090242866beba869b2aacb09` inspected but not ported in this slice | In v0.21.0-shaped `plugins/disk-cleanup`, new durable scripts under `$HERMES_HOME/scripts` could be auto-tracked from `write_file`, while stale/manual entries for either the active or a named profile's scripts tree—including the tree root—could still become dry-run deletion candidates or be deleted when their stored category was old `test` / `temp` state. Focused RED evidence: `test_write_file_durable_scripts_test_file_persists` deleted an active-profile script; `test_quick_drops_old_temp_entry_under_active_profile_scripts` deleted stale `temp` state; `test_dry_run_omits_old_temp_entry_under_nested_profile_scripts` previewed stale nested-profile script deletion; `test_quick_drops_tracked_script_root_without_deleting_it` deleted both active and named-profile script roots. | `KEEP` | Bundled `disk-cleanup` plugin only; no wildcard tracking machinery ported. Live config is known from controller read-only inspection to enable this plugin, but no live config values were read or exposed here. | This slice | Focused RED/GREEN cases named in the symptom column; full affected file: `HERMES_PYTHON=/home/brian/.hermes/hermes-agent/.venv/bin/python scripts/run_tests.sh tests/plugins/test_disk_cleanup_plugin.py` (32 passed); direct temp-home integration preserved both durable trees and deleted a disposable temp; independent Codex blocker review passed with no P0/P1 findings. | Checkpoint `brian-rebuild-v0.21.0-disk-cleanup-scripts`; revert this slice to remove the behavior. |
