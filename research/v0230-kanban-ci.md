# v0.23.0 research: kanban, CI and test-harness fork features

Ticket: brianfaires/hermes-shared#54 (map #52). Baseline `3d5a9416f2`, base `v0.22.5`, target `v0.23.0`.
Method: value gate then placement gate. Evidence is read-only; keys, paths and counts only.
Conflict claims come from `git merge-tree --write-tree --merge-base=<commit>^ v0.23.0 <commit>` (a cherry-pick dry run). Nothing was executed against v0.23.0; tests named below are not run, so "covered" means by source reading.

## Commit grouping

`8e4168082d` and its three fixups hold unrelated behaviors. The fixup subject lines claim the kanban commit, but the content is not kanban.

| Commit | Content | Feature |
|---|---|---|
| `8e4168082d` `kanban_db.py`, `test_kanban_scheduled_resume.py` | `unblock_task(expected_scheduled_run_id, note)` fence | F1 |
| `8e4168082d` `session_local.py`, `test_session_end_delegations.py` | `LocalSessionAdapter.gateway_runner` | F2 |
| `e9eb8b03e2` `cron/*`, `self_removal.py`, `cron-internals.md`, `test_script_claim_heartbeat.py` | script-subprocess self-removal receipt | F3 |
| `e9eb8b03e2` `status.py`/`status_command.py` (informational probes) + `af85eafc90` + `86fed153b9` | strict gateway process identity | F4 |
| `e9eb8b03e2` `update_receipt.py`, `test_update_receipt.py` | fleet `served_profiles` identity normalization | F5 |
| `1d1f4ceab1` three `tests/agent/*` | mock repair for the skill-budget feature (`97455dd381`) | fold into skills |
| `1d1f4ceab1` `test_tests_tree_layout.py` | registers `tests/scenarios` | fold into F7 |
| `ac0153a3d1` | org workflow scenario test | F7 |
| `a436856eb6` workflows, exclusions, `required_results.py`, `tests/ci` | fork CI policy | F6 |
| `a436856eb6` six fixture files | flaky-test repairs, owned by other features | F8 |
| `86fed153b9` | test fixes forced by F4's stricter matcher | travels with F4 |
| `47fedba3f2` | three unrelated test fixes | F8 |
| `c0bba7ec16` | `.gitignore` `.scratch/` | F9 |

## F1. Fenced CI resume: `unblock_task(expected_scheduled_run_id=, note=)`

- **What**: a CI observer may unblock only the exact parked attempt (`scheduled` status, no sticky block, latest `scheduled` event `run_id` matches), and posts a system comment atomically. Replays, newer attempts and manual holds return False.
- **v0.23.0**: not covered. `hermes_cli/kanban_db.py:3557` is still `unblock_task(conn, task_id)`. `_cmd_unblock` (`kanban.py:1020`) is a human CLI with no fence. `schedule_task(expected_run_id=)` exists upstream, so only the resume half is missing.
- **Consumers (live)**: `hermes-extensions/plugins/kanban-ci-wait` (v0.2.0), installed pinned in `profiles/ang/plugins/` and listed under `plugins.enabled` in `profiles/ang/config.yaml`. It calls `kb.unblock_task(..., expected_scheduled_run_id=, note=)` and refuses to park unless `inspect.signature` shows both kwargs. Its README declares the host requirement. Skills: `profiles/ops/skills/kanban/kanban-state-integrity`, `profiles/ang/skills/orchestration/agent-worktree-orchestration`, `skills-library/.../kanban-orchestrator/references/decomposition.md`. Ang logs mention it in 8 files, ops and rook in 1 each. No standing CI-wait cron job exists now (the 4 + 1 + 14 + 2 jobs in default/ang/ops/rook `jobs.json` have none); observers are created and removed per wait.
- **Why not a plugin**: the fence and the unblock must share one transaction. `write_txn` (`kanban_db_connect.py:1240`) raises on nesting unless `allow_nested=True`, and `unblock_task` opens its own. A plugin would have to re-implement unblock's run reclaim and parent re-gate in SQL against private tables.
- **Disposition**: keep. **Placement**: minimal core seam, about 15 lines on one function, plus its 73-line test. Good upstream-PR candidate (generic "fenced resume of a parked attempt").
- **Rebuild**: `kanban_db.py` applies without conflict. Preserve the dropped comment `# Re-gate on parent completion...` that the fork diff deleted.

## F2. `LocalSessionAdapter.gateway_runner`

- **v0.23.0**: covered. `gateway/session_local.py:29` has `self.gateway_runner = getattr(authority, 'runner', None)` with a comment describing the same failure (push wake skips the authority FIFO). Upstream also extended the adapter (`kept_source`, `forget`, detached routes).
- **Consumers**: none beyond the kanban wake path that upstream now serves.
- **Disposition**: drop-upstream. Cherry-pick conflicts in `session_local.py`, which confirms upstream rewrote it.
- **Test**: re-run `test_kanban_wake_rebuilt_local_source_enters_the_owner_fifo` against v0.23.0 as a validation check. Carry it only if it fails. The `runner=runner` fixture edit in `test_multiplex_transport_matrix.py` (in `a436856eb6`) is also obsolete; upstream changed that file by 60 lines.

## F3. Cron script-subprocess self-removal receipt

- **What**: a cron script that removes its own job (the CI observer does, then its final delivery must still go out) acknowledges removal through a private temp receipt passed via `_HERMES_CRON_SELF_REMOVAL`. External deletion and replacement-record cases stay refused. The in-process ContextVar marker moved out of `cron/jobs.py` into new `cron/self_removal.py` (90 lines), with hooks in `remove_job`, `fire_claim_fence`, `run_one_job` and `_run_job_script`.
- **v0.23.0**: only the in-process marker exists (`cron/jobs.py:146-165`, `scheduler.py:525,2881`, `cancellation.py:181`). `scheduler_script.py` passes nothing to the subprocess, so `remove_job` in a script child never sets the parent's marker and `self_removal_delivery_allowed` stays False. Same as v0.22.5, so this gap is real.
- **Consumers (live)**: `kanban-ci-wait` observer, a `no_agent` cron script with `deliver="local"` that calls `remove_job` (`kanban_ci_wait.py:118`). No other script removes its own job (grep of cron jobs, plugins, skills).
- **Disposition**: keep only as long as the observer self-removes. **Placement**: core patch (about 100 lines, 3 hook sites). `cron/jobs.py` conflicts, since v0.23 reshaped that file (363 changed lines vs v0.22.5).
- **Cheaper alternative (open Q1)**: change the observer to `pause_job` or `update_job` itself instead of `remove_job`, and sweep spent jobs later. The fire claim stays valid so delivery needs no receipt. That moves the whole feature into `hermes-extensions` and removes the core patch, at the price of retained paused jobs. Not tested.

## F4. Strict gateway process identity

- **What**: `gateway_process_subcommand` matches the executed program (python option operands, `-m`, launcher argv, legacy `HOME=... cmd` records), not any token. Excludes `--help`, `-h`, `--print-runtime-command`. Fixes `compileall` over a file list containing `gateway/run.py` being classed as a gateway. Moves the matcher into new `gateway/status_command.py`.
- **v0.23.0**: not covered. `gateway/status.py:541` `_gateway_command_subcommand` still does `any(t.endswith("/gateway/run.py") for t in tokens)` and has no informational-probe exclusion. Upstream added `gateway/status_inline_source.py` (`python -c` peeling) and a `test_status_command.py` tweak, which handle the inline case only. A bare operand containing `gateway/run.py` is still a false positive.
- **Consumers**: the pre-update pause/`gateway run --replace` scans and the test live-system guard. Incident evidence is only the commit message ("live deployment caught compileall processes classified as gateways"); logs show 0 `compileall` hits.
- **Disposition**: keep, medium value, but only if Brian wants it. A misclassified process makes update/replace logic wait on or signal a non-gateway. **Placement**: core patch confined to `status.py` (about -35 lines, +1 delegation) plus the new module; upstream-PR candidate.
- **Rebuild**: `status.py` conflicts (modify/delete on `status_command.py` and `test_process_information.py`, which v0.23 never had). `86fed153b9` (`live_system_guard.py::_gateway_spawn_intent`, argv-shape edits in `test_gateway_command_line_matcher`, `test_stderr_timestamp`, `test_update_pause_*`) exists only because F4 tightened the matcher; it ships inside F4's commit or is dropped with it. `test_update_pause_unmapped_home_proof.py` conflicts.

## F5. Fleet `served_profiles` identity normalization

- **What**: the session-runtime socket identity serves `[{'profile_id': home, 'home': home}]` (`gateway/session_authorities.py:136`, same in v0.22.5 and v0.23.0). `_fleet_row` accepts only name strings, so the dicts are dropped and multi-profile restart coverage in the update receipt cannot be checked. The fork maps validated homes back to profile names and rejects foreign or mismatched ids.
- **v0.23.0**: not covered. `update_receipt.py:1194` passes `identity.get("served_profiles")` unchanged.
- **Consumers**: `hermes update` fleet receipt on the live multiplexed gateway (ang/ops/rook profiles served by one gateway).
- **Disposition**: keep, but it belongs to the update-receipt feature set (map ticket #56 area). Fold it there. **Placement**: core patch, about 20 lines in one file, plus 30 test lines. Upstream-PR candidate (upstream's own producer and consumer disagree).
- **Rebuild**: no conflict (`update_receipt.py` changed 2 lines upstream).

## F6. Fork CI policy (headless Ubuntu suite)

- **What**: on `brianfaires/hermes-agent`, `ci.yaml` runs a 4-slice `headless-tests` job on `ubuntu-24.04` via `scripts/run_tests.sh` with `scripts/ci/headless-test-exclusions.txt` (76 effective entries: unused messaging platforms and desktop-build suites). `workflow_dispatch full_qualification` or `release` restores upstream's broad pipeline. `required_results.py --headless-tests` makes the headless result mandatory. `docker.yml` and `nix.yml` swap Blacksmith labels and actions for GitHub-hosted ones so they execute on the fork. Tests: `tests/ci/test_headless_test_selection.py` (124 lines), `test_required_results.py`.
- **v0.23.0**: not covered. v0.23.0 `.github/workflows/README.md` states that fork behavior is to skip repo-gated jobs, and Blacksmith labels still dominate (243 occurrences vs 225). `ci.yaml`, `docker.yml`, `nix.yml` and `required_results.py` are byte-identical between v0.22.5 and v0.23.0, so the commit applies without conflict. `run_tests.sh` already supports `HERMES_TEST_SLICE`, `--file-retries`, `--file-timeout`. `setup-pm` still takes `packages`.
- **Consumers (live)**: fork `CI` runs on 2026-11-02 (push to `main` and PRs) passed all 4 headless slices; broad lanes show `skipped`. Fork issue #204 ("execute Docker and Nix tests in the fork") is merged.
- **Disposition**: keep. **Placement**: core patch, but CI-only (no runtime effect). The least divergent variant is a fork-only workflow file plus the exclusions file, leaving `ci.yaml` untouched and disabling upstream workflows in repo settings. That loses the single `all-checks-pass` gate and the `--headless-tests` rule (open Q2).
- **Risks to check on rebuild**: (a) 76 exclusions: only `tests/scripts/desktop_update` changed in size (30 to 38 files, all still excluded); every other ignored directory is identical, so no exclusion is dead. Some `--ignore-glob` entries were not verified against v0.23.0 file names. (b) `tests/ci/test_workflow_job_graph.py` and `test_update_ci_routing.py` assert workflow structure and were not run against the edited `ci.yaml`. (c) Scheduled `Canary Release` runs on the fork sit `queued`/`pending` on unavailable Blacksmith runners; gating `canary-release.yml` on the repository would fix it but is outside this commit.

## F7. Shared-board orchestration scenario test

- **What**: `tests/scenarios/test_kanban_orchestrator_flow.py` (85 lines), `tests/scenarios/README.md`, a `tests/AGENTS.md` pointer and the `scenarios` entry in `tests/test_tests_tree_layout.py`. Real board and dispatcher with scripted workers; covers Sunny to Rook plan, Ang/Ops work, dependency-gated milestone, close-out notification (shared ADR 0001, #173, #200).
- **v0.23.0**: no `tests/scenarios/`. Every API it calls exists: `kanban_db_connect.connect_closing`, `kanban_db_dispatch.dispatch_once`, `kanban_db_notify.add_notify_sub/list_notify_subs/unseen_events_for_sub`. Applies without conflict.
- **Consumers**: `~/.hermes/shared/docs/adr/0001-sunny-rook-handoff-follows-upstream-orchestration.md` is the policy source; default CI discovers the directory.
- **Disposition**: keep. **Placement**: tests only (in-tree, no runtime divergence). It is the only regression net for the board contract the profiles rely on, so it earns its place across rebuilds. Could move to hermes-shared, but it imports host kanban modules and uses host conftest fixtures (open Q3).

## F8. Flaky-test fixture repairs

| Change | Disposition |
|---|---|
| `test_openrouter_compat_provider.py` offline catalog patch (`47fedba3f2`) | drop-upstream: v0.23.0 line 812 already patches `requests.get` offline |
| `test_multiplex_transport_matrix.py` `runner=` (`a436856eb6`) | drop-upstream (F2 and upstream rewrite) |
| `test_cron_next_run_profile_timezone.py` DST parametrization (`a436856eb6`) | re-evaluate: conflicts with upstream's 5-line edit; keep only if the test still fails |
| `test_commandcode_profile`, `test_telegram_update_admission`, `test_session_profile_db`, `test_undo_command`, `test_left_core_migration` (`a436856eb6`) | not upstream-changed; run each on v0.23.0 and keep a fix only if the test fails, folding it into the owning feature's slice |
| `test_worktree_selfheal.py` stderr note (`47fedba3f2`) | drop-low-value (diagnostics only) |
| `test_hosted_room_driver_runtime.py` removes `lease_ttl_seconds=0.4` (`47fedba3f2`) | re-evaluate: `hosted_room_driver.py` still takes `lease_ttl_seconds` (default 30 s); depends on whether the hosted-room feature is kept |
| three `tests/agent/*` skill-budget mocks (`1d1f4ceab1`) | fold into skill-budget feature `97455dd381`; `agent/skill_prompt_settings.py` does not exist in v0.23.0, so drop with it if that feature drops |

Rule for the rebuild: no standalone "fixtures" slice. Run the suite on the rebuilt tree and attach each needed repair to its owning slice.

## F9. `.scratch/` in `.gitignore`

- v0.23.0 has no `.scratch/` entry (baseline has it at line 1). No skill or profile skill in `skills-library` or `profiles/*/skills` mentions `.scratch`. **Disposition**: drop-low-value, or fold as a one-line change into whichever commit first touches `.gitignore`. Merges cleanly. Open to keep if Ang's repo workflow writes `.scratch` in the checkout (source commit authored by Ang).

## Summary

| Feature | Disposition | Placement |
|---|---|---|
| F1 fenced CI resume | keep | minimal core seam (upstream PR candidate) |
| F2 local adapter runner | drop-upstream | none |
| F3 script self-removal receipt | keep, pending Q1 | core patch, or plugin-only if Q1 accepted |
| F4 strict gateway process identity | keep (medium value) | core patch (upstream PR candidate) |
| F5 fleet identity normalization | keep, fold into update/receipt work (#56) | core patch (upstream PR candidate) |
| F6 fork CI policy | keep | core patch in CI files (or fork-only workflow, Q2) |
| F7 orchestration scenario test | keep | in-tree tests only |
| F8 fixture repairs | drop-upstream / re-evaluate by running tests | fold into owners |
| F9 `.scratch` ignore | drop-low-value | none |

## Open questions for Brian

1. F3: accept moving the observer from `remove_job` to pause-and-sweep, so the 100-line cron core patch disappears? Or keep the receipt in core because removal-after-resume is the intended lifecycle?
2. F6: keep the in-place `ci.yaml` edit (single gate) or move to a fork-only workflow file to stop touching upstream's most-edited file each release?
3. F7: stay in-tree under `tests/scenarios/`, or move to hermes-shared with a runner that points at a hermes-agent checkout?
4. F4: is the compileall false positive worth a core patch, or acceptable to drop and let `compileall` runs be ignored operationally?
