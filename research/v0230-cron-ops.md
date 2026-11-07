# v0.23.0 research: cron, goals, tools and ops fork features (hermes-shared #57)

Baseline `3d5a9416f2` (= `v0.22.5` + 37 fork commits), target `v0.23.0`. Ten commits, no `fixup!` in range (the fixups in the baseline belong to the kanban commit). Mixed commits split by behavior: the goals commit holds two fork features, so there are 11 fork features. Upstream references are `git -C ~/.hermes/hermes-agent show v0.23.0:<path>`. Live evidence cites key names, paths and counts only. "Contract run" means I extracted `v0.23.0` to a scratch dir and ran the old behavior with the repo venv.

## Summary

| # | Fork feature | Commit | v0.23.0 | Disposition | Placement |
|---|---|---|---|---|---|
| 1 | Retained cron execution results | `0419b1788b` | not covered | drop-low-value (pending Q1) | none; if wanted, plugin owns it |
| 2 | Cron script user-bus env | `13a18373a3` | helper exists, script path unwired | keep (cheap) | core patch, 1 line; upstream PR |
| 3 | Goal wait recovery (parked goals wake) | `d0bfe68f7b` (part) | not covered | keep | core patch, new gateway module + 4 seams |
| 4 | Goal judge cannot overwrite newer controls | `d0bfe68f7b` (part) | not covered | keep | core patch in `hermes_cli/goals.py` |
| 5 | Platform tool-schema pins | `794d8289c8` | global `defer` only | drop-low-value | config (`tools.tool_search.defer`) if needed |
| 6 | Spoken TTS survives compression | `38959305f5` | not covered | keep | core patch (new module + 3 hooks) |
| 7 | Redact terminal output before selecting previews | `2107ff2604` | not covered (contract fails) | keep | core patch; upstream PR candidate |
| 8 | Webhook transform tree kill on timeout | `dd7934306b` | not covered | keep reduced | core patch: swap to upstream `bounded_probe_run` only |
| 9 | Backup banner shows archived root | `35063c8335` | not covered | drop-low-value | none (optional upstream PR) |
| 10 | Debug port probe on available loopbacks | `b44c373f04` | not covered, bug live on host | drop-low-value | none (optional upstream PR) |
| 11 | Checkpoint abandoned index-lock recovery | `3b70772f62` | root cause fixed | drop-low-value | none |

## 1. Retained cron execution results (`0419b1788b`)

- **Commit/behavior.** Adds `cron/execution_results.py` (table `execution_results` in each profile's `cron/executions.db`) and threads `final_response` through `executions.py` terminal writes, `scheduler.py`, `scheduler_execution_receipts.py`, `scheduler_bookkeeping.py`, `scheduler_authority.py`. Rows hold a job snapshot, the terminal record and the full final response, and survive the 1000-row prune. Folds the #175/#186 legacy-schema compat (two source commits).
- **v0.23.0.** Not covered: no `execution_results` symbol. Upstream `cron/executions.py:33 MAX_TERMINAL_EXECUTIONS = 1000` and `_prune_unlocked` still drop old rows. Upstream does write each run's output to `cron/jobs.py:save_job_output` (files under `cron/output/`), plus `executions.list_executions/get_execution`.
- **Live consumers.**
  - Only reader: `~/.hermes/hermes-extensions/plugins/cron-calendar-mirror` (`mirror.py:902` imports `cron.execution_results.iter_execution_results`). Its README says "Nothing in this candidate enables the plugin or installs a periodic invocation". It is not in `plugins.enabled` of any profile and not under `~/.hermes/plugins` or `profiles/*/plugins`. No timer, no cron job and no log line mentions it. `~/.hermes/cron/calendar_sync.json` was last written Jul 25, before the plugin snapshot (2026-10-26).
  - The fork code runs for every profile regardless: `execution_results` row counts (rows with a terminal record) are default 995, ang 1469, ops 39542, rook 4908. It is unbounded and stores full final responses.
- **Disposition.** drop-low-value for core: the only consumer is an unenabled candidate, and the table grows without bound. If Brian wants the mirror, move retention to the plugin (placement: profile plugin) using upstream `list_executions` plus the `cron/output` files; that removes the core patch.
- **Rebuild.** Large patch across the state-critical ledger; upstream moved `cron/executions.py` (+109 lines for `missed` status) and `scheduler.py` (~-150), so replay would conflict at every terminal-write site. Dropping removes the conflict. Plugin needs a one-line import change.
- **Open Q1.** Is the calendar mirror to be turned on? If yes, plugin-owned retention; if no, drop it. Old rows in the four DBs stay inert either way.

## 2. Cron script user-bus environment (`13a18373a3`)

- **Behavior.** `cron/scheduler_script.py` wraps the script env in `systemd_user_bus_env(...)`, so a script's `systemctl --user` reaches the owning user's bus.
- **v0.23.0.** `tools/process_registry.py:systemd_user_bus_env` exists and is used by `cron/scheduler.py:3700` (agent worker) and `hermes_cli/kanban_db_dispatch.py:2875`, but not by `scheduler_script.py:482` (still `build_subprocess_env(strip_launch_profile=True)`). Contract run: with `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` already in the ambient env, `build_subprocess_env` keeps both. The fork only changes the result when the ambient env lacks them or has the wrong bus.
- **Live consumers.** One enabled ops cron script calls `systemctl --user`: job `053953` (`profiles/ops/scripts/ops_heartbeat.py`, 3 refs). The live gateway process has both variables set (read from `/proc/<pid>/environ` names only). `profiles/*/logs/agent.log.1/.2` contain past "Failed to connect to bus" lines (agent shell, not proven cron).
- **Disposition.** keep. Core patch, one line plus its test; consistent with the two other upstream call sites. Upstream PR candidate. Value is resilience, not a current failure; drop-low-value is defensible if the gateway always runs from the systemd unit.
- **Rebuild.** `scheduler_script.py` changed 6 lines upstream; the hunk still applies.

## 3. Goal wait recovery (`d0bfe68f7b`, wait half)

- **Behavior.** Parked messaging goals wake without another user message. New `gateway/run_goal_waits.py` (170 lines) polls persisted waits through the existing heartbeat poller and admits one continuation with a stored `waiting_wake_id`; invalidated by pause/clear/replace. Seams: `run_goals.py` poll loop (+2), `run_heartbeat_acceptance.py` (`automation_owner_is_current`), `run_turn.py` (1 call), `session_automation.py:admit_route_wake(identity=)`, `session_ingress.py:execute_admission` (currency check). Plus `GoalState.waiting_wake_id` and `is_waiting(clear_satisfied=)` in `goals.py`.
- **v0.23.0.** Not covered. No `waiting_wake_id` anywhere; `GoalManager.is_waiting()` (`hermes_cli/goals.py:1413`) still just clears a satisfied barrier and relies on the next user message. `run_goals.py` poller handles heartbeats/loops only.
- **Live consumers.** Goals are used: `state_meta` keys `goal:*` count 59 (default; 20 active, 24 done, 13 paused, 2 cleared), ang 3, ops 2, rook 1. No `goals.*` config keys in any profile. Zero goals currently parked (no `waiting_*` fields) and no goal-wait log lines, so the wake path has no observed use in the data I could read.
- **Disposition.** keep (value to confirm, see Q2). Core patch, no lower placement: needs `GoalState` persistence and gateway admission.
- **Rebuild.** `hermes_cli/goals.py` is unchanged v0.22.5 to v0.23.0, so that part replays cleanly. The gateway seams all still exist, but upstream rewrote `run_turn.py` (+264/-), `session_automation.py` (+253), `session_ingress.py` (+87), and the poller loop in `run_goals.py` now calls `restore_heartbeat_watches`; re-seat the five hooks by hand. New module and tests are standalone.

## 4. Goal judge cannot overwrite newer controls (`d0bfe68f7b`, authority half)

- **Behavior.** `save_goal(expected_state=)` compare-and-swap; `evaluate_after_turn` judges a copied snapshot, discards its verdict if pause/clear/replacement landed meanwhile, and continues with updated criteria. Docs lines in `goals.md`.
- **v0.23.0.** Not covered: `save_goal(session_id, state)` (`goals.py:657`) is still an unconditional write; no `_evaluation_snapshot`.
- **Live consumers.** Same 59-goal footprint; 13 paused and 2 cleared goals show controls are used. No log evidence of the race.
- **Disposition.** keep. Core patch in `hermes_cli/goals.py` only (clean replay, file untouched upstream). It is independent of #3; land it first. Could ship as an upstream PR.
- **Open Q2.** Keep #3 given no parked goals exist today, or drop #3 and keep only #4?

## 5. Platform tool-schema pins (`794d8289c8`)

- **Behavior.** `tools.tool_search.pinned.<platform>: [tool names]` keeps listed schemas visible instead of deferred; pin choice persisted in `schema_pins` with the session tool snapshot and restored on resume. Touches `model_tools.py`, `agent_init.py`, `tool_search.py`, `tool_search_pinning.py`, `mcp_tool_agent.py`, `hermes_state_worker_context.py`, `config_defaults.py`.
- **v0.23.0.** Partly covered: `tools/tool_search.py:54 ToolSearchConfig.defer_tools`, read from `tools.tool_search.defer` (global list; `[]` keeps every tool eager). Not per platform, and the session store has no `schema_pins`.
- **Live consumers.** None. `tools.tool_search` exists in default and ops configs with only `enabled`, `max_search_limit`, `search_default_limit`, `threshold_pct`; no `pinned` and no `defer` key in any profile config. No plugin or cron job references it.
- **Disposition.** drop-low-value. Largest cross-cutting patch of the set (11 files, session-store schema) with zero consumers. Use `tools.tool_search.defer` (config) if a tool must be eager.
- **Rebuild.** Removing it avoids touching `model_tools.py` and the runtime session store (`pin` shape change).

## 6. Spoken TTS survives compression (`38959305f5`)

- **Behavior.** `agent/compression_speech.py` plus hooks in `context_compressor.py` (`_serialize_for_summary`, `_prune_old_tool_results` arg truncation, `_build_static_fallback_summary`): `text_to_speech` text is kept as `[Spoken assistant output]`, delivery paths dropped, duplicates collapsed.
- **v0.23.0.** Not covered: only a summary template `context_compressor.py:1971` (`[text_to_speech] generated audio (N chars)`), which discards the spoken text. The hooked functions still exist (`_prune_old_tool_results:3274`, `_serialize_for_summary:3437`, `_build_static_fallback_summary:3502`); upstream's `micro_compaction.py:277` also calls `_serialize_for_summary`.
- **Live consumers.** Real use: default `state.db` has 83 `text_to_speech` tool results (28 in the last 30 days), ang 2, ops/rook 0; compression is active (31,577 compacted messages in default). `tts.provider` is set in default, ang, ops configs. Matches the existing `hermes-fork-dev/references/voice-tts-compression-preservation.md` justification.
- **Disposition.** keep. Core patch: the compressor has no hook seam, so the smallest form is the new module plus three one-line hooks. Upstream PR candidate.
- **Rebuild.** `context_compressor.py` moved about 155 lines; expect small hunk drift at the three hooks. New module and tests replay as-is.

## 7. Redact terminal output before selecting previews (`2107ff2604`)

- **Behavior.** Masks the complete output before any window is chosen (foreground head/tail, process previews, rolling-buffer eviction incl. PEM boundaries, notifications, delegated child output, receipts). New: `agent/redact_partial.py`, `tools/environments/output_redaction.py`, `tools/process_registry_output.py`; edits in `process_registry.py`, `base.py`, `base_output.py`, `run_notifications.py`, `session_ancillary.py`, `delegate_tool_child_run.py`, `agent/redact.py`.
- **v0.23.0.** Not covered for the boundary case. Upstream redacts after selection (`tools/process_registry_results.py:55-57` takes `output_buffer[-MAX_OUTPUT_CHARS:]` then `redact_terminal_output`; `process_registry.py:2632 _redact_process_result` runs after the transform). Contract run on the extracted tree: a PEM private key redacts fully when whole, but a tail slice that dropped the `BEGIN` line leaks the key body through `redact_terminal_output(force=True)`. Upstream `agent/redact.py` grew 700 lines but has no window or partial-boundary handling.
- **Live consumers.** Security invariant for all surfaces; no config key. `security.redact_secrets` governs it. Every profile runs terminal and background processes.
- **Disposition.** keep. Core patch, upstream PR candidate (security). No lower placement: the order of select and redact sits inside process registry and environment collectors.
- **Rebuild.** Highest conflict in this set: upstream changed `process_registry.py` (82 lines), `run_notifications.py` (145), `session_ancillary.py` (97), `base.py` (28), `delegate_tool_child_run.py` (23), `redact.py` (+700). Re-derive from the contract tests (`test_output_redaction_before_selection.py`, `test_process_output_retention_redaction.py`) rather than replay; schedule it after the simpler slices.

## 8. Webhook transform tree kill on timeout (`dd7934306b`)

- **Behavior.** `webhook_filters.py` runs transform scripts through `bounded_probe_run` instead of `subprocess.run`, and `bounded_probe_run` sweeps the private process group (`killpg`) after the leader is reaped.
- **v0.23.0.** Partly covered. `gateway/platforms/webhook_filters.py:193` still uses `subprocess.run` (timeout kills only the direct child). Upstream `hermes_cli/_subprocess_compat.py:825 bounded_probe_run` already supports `input=`, `raise_on_spawn_failure`, `process_group=0`, `kill_and_drain`, and `kill_process_tree`. Missing in upstream: the post-reap `killpg` sweep on the success path.
- **Live consumers.** One dynamic subscription in `~/.hermes/webhook_subscriptions.json` has a `script` key; default config has `platforms.webhook.extra` with `script_timeout_seconds` (0 static routes); ang/ops/rook have none. Webhook adapter is active (64,295 `[webhook]` lines in `logs/gateway.log` + `.1`) with zero "script timed out" lines.
- **Disposition.** keep reduced: change only `webhook_filters.py` to call upstream `bounded_probe_run` (about 10 lines); drop the `_subprocess_compat.py` sweep, which alters a shared helper used by git probes and gains little for operator-owned transforms. Drop the doc note about detached children.
- **Rebuild.** Applies cleanly; `webhook_filters.py` unchanged upstream. Dropping the helper change avoids conflict with upstream's new `kill_process_tree(grace=)`.

## 9. Backup banner shows archived root (`35063c8335`)

- **Behavior.** `hermes_cli/backup.py` prints the installation root instead of the active profile home in the "Scanning ..." banner and external-memory notice.
- **v0.23.0.** Not covered: `backup.py:622,667` still call `display_hermes_home()` with no argument, while `hermes_root = get_default_hermes_root()` is in scope. Cosmetic only; the archive contents are already correct.
- **Live consumers.** None scripted (no cron job or script calls `hermes backup`; `~/.hermes/backup-jobs/` holds only `config.json*`).
- **Disposition.** drop-low-value. Optional two-line upstream PR.

## 10. Debug port probe on available loopbacks (`b44c373f04`)

- **Behavior.** `find_free_debug_port` skips loopback families that cannot bind, so an IPv4-only host still finds a free port.
- **v0.23.0.** Not covered: `browser_connect.py:1027-1038` unchanged logic. On this host `AF_INET6` socket creation fails (errno 97), so upstream always falls to `preferred + 1`. Real bug here, impact small.
- **Live consumers.** Only the interactive `/browser connect` paths (`cli_commands_mixin.py:259`, `tui_gateway/methods_browser.py:60`). No log hits; configs use `browser.cloud_provider`/`auto_local_for_private_urls` and no `browser.cdp_url`.
- **Disposition.** drop-low-value; optional small upstream PR (about 10 lines plus test). Revisit if `/browser connect` is used.

## 11. Checkpoint abandoned index-lock recovery (`3b70772f62`)

- **Behavior.** New `hermes checkpoints recover-index-lock <workdir> [--apply]` quarantines one project's abandoned index lock after two writer checks (`tools/checkpoint_index_locks.py`, 60 lines; docs).
- **v0.23.0.** Root cause fixed: `tools/checkpoint_manager.py:_stage_all_args` excludes the store and its lock from `git add -A` (the store-inside-HERMES_HOME growth that led to the timeout that left `indexes/<hash>.lock`; the function is absent in v0.22.5). No equivalent manual recovery command; `hermes_cli/gitlock.py` handles repo index locks, not the checkpoint store.
- **Live consumers.** `checkpoints.enabled` true in default and ops. No `.lock` under `~/.hermes/checkpoints`, no `quarantined-index-locks` dir, so the command has never been applied here.
- **Disposition.** drop-low-value (root cause upstream, manual command unused). Recovery ref: `3b70772f62`; reintroduce only if a lock reappears.

## Open questions for Brian

1. Q1: Enable the cron calendar mirror? Decides #1 (drop vs plugin-owned retention) and whether the plugin import is repointed.
2. Q2: Keep goal wait recovery (#3) with no parked goals present, or keep only #4?
3. Q3: Accept #2 as a one-line patch, or treat the gateway unit environment as sufficient and drop it?
4. Q4: Which of #2, #4, #6, #7 (and optional #9, #10) go to upstream PRs, and who carries them?

## Rebuild order suggestion

Clean replays first: #4, #8 (reduced), #2. Then #6 (small drift). Then #3 (re-seat gateway hooks). #7 last (re-derive). Dropped: #1 (pending Q1), #5, #9, #10, #11.
