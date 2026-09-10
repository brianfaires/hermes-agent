# Independent Codex blocker review — v0.21 restoration

Reviewer: native independent Codex subagent `/root/independent_blocker_review`; no implementation authorship and no source/test/index changes. Review performed against base `cf62291dad2cd9c12de80bc5194ff76ef0f6ce55` through `9d2d5867214a3156f645ed6fce6353284f17d6a4`. Source correction and final ledger follow-up pending below.

## Initial decision: one acceptance blocker

**R1 / high — restart can remain draining indefinitely after successful caller delivery.** `gateway/run.py:12495-12508` registers `delivered.set` using the existing callback chaining interface. `gateway/platforms/base.py:5549-5568` executes prior callbacks first. The adapter bounds the complete chain at `:7189-7195`; if a preexisting async callback stalls, cancellation skips the new restart callback. The new unbounded `await delivered.wait()` never completes, leaving the gateway draining with restart already marked requested.

Executable reproduction used the existing real-adapter `test_model_restart_waits_through_real_adapter_delivery` unchanged, installed a same-generation callback awaiting `asyncio.sleep(30)` before restart registration, and patched only `_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS` to `.02`. Caller delivery completed, but waiting for `_restart_task` raised `TimeoutError`; traceback stopped at `gateway/run.py:12508`, `await delivered.wait()`. No live delivery/restart occurred. The suite reported one error. This is not an approval-gated scope request: it is a blocker in the new delivery barrier.

Requested fix: signal delivery independently of a preceding callback's completion, preserving generation ownership, existing callback behavior, restart authorization/audit and caller delivery ordering. Parent proposed a narrow optional prepend argument on the existing registry, used only for restart signaling. A real-adapter stalled-callback regression must pass before R1 closes.

## Independent validation completed

Canonical interpreter `/home/brian/.hermes/hermes-agent/.venv/bin/python -B`, worktree PYTHONPATH, fresh temporary HERMES_HOME and HERMES_BUNDLES_DIR, bytecode disabled, and inherited Hermes/provider/platform settings removed. Separate processes ran `-m unittest discover -s <directory> -p <filename> -v`:

- `tests/plugins/test_v021_cleanup_safety.py`: 2 passed.
- `tests/cron/test_prompt_path_scanning_fc13.py`: 8 passed.
- `tests/gateway/test_v021_restart_delivery.py`: 3 original tests passed; independent stalled-callback variant reproduced R1.
- `tests/gateway/test_v021_webhook_process_cleanup.py`: 2 passed, including a real disposable child/grandchild process tree.

Inspected all candidate production diffs and added tests, root's `../qualification-final.json`, and `../critical-gates.md`. Checked actual installed Discord `ForumChannel.create_thread` signature: `suppress_embeds` is accepted. Confirmed hosted `.github/workflows/tests.yml` discovers test files through `scripts/run_tests_parallel.py` and executes per-file pytest via `scripts/run_tests.sh`. No hosted run or local pytest collection claimed.

No other concrete source acceptance blocker found in the bounded pass. Cleanup refusal preserves existing protected-tree rules; core cron validation imports scanner at call time and real jobs/tool/API/fire-time paths pass; authored file strict checks remain separate from looser skill content. Worker changes reuse current ownership predicates. Board inventory uses an explicitly read-only SQLite connection and leaves task DB pin behavior intact. Skill invalidation adds external manifest state to fresh builds, without directly mutating existing agent prompt state. Process cleanup signals its own new process group and preserves transform path restrictions. Gated identity/routing/Tirith/rollback/policy changes are deliberately outside this review's requested implementation scope.

## Limitations

No full suite, hosted CI, live integration, real provider TTS, Windows process-tree validation, installation or dependency solver execution. Root's broader qualification is author evidence, distinct from the independent 15 passing tests above. No parity claim for approval-gated or unqualified subfeatures. Final ledger/document coverage review and targeted R1 correction review will be appended rather than replacing this independent record.

## Targeted R1 correction — closed

Reviewed `10081c3099c814c8e5f8c6e0f8f83d2b9881ac40`. Optional `prepend=False` preserves existing registration order by default; restart's `prepend=True` signals the delivery boundary before unrelated async callback work. Existing generation checks remain unchanged. Independently reran the restart file with the same isolated interpreter/environment: all 4 tests passed, including `test_prior_callback_timeout_cannot_strand_restart`. R1 is closed.

## R2 / high — busy slash restart binds the prior turn instead of its own delivery

Same restart-boundary review found another affected entrypoint: `gateway/platforms/base.py:6354-6383` dispatches `/restart` inline when a session is already busy. New `gateway/slash_commands.py:1815-1820` passes that session key to `request_restart`, which binds the existing agent turn's callback. The inline slash response is delivered separately. Completing the previous turn can release restart while the command response is still sending.

Independent executable reproduction used the real `BasePlatformAdapter.handle_message` busy-command branch and real `GatewayRunner.request_restart`; the adapter send was a blocked disposable async fixture, active-work draining and stop were mocked (no real teardown), and generation 7 represented the existing turn. After the slash send began, firing generation 7's callback produced `SLASH_SEND_PENDING True RESTART_STOP_CALLED 1`. The original slash unit test only checks a mocked `request_restart` argument and misses this path. Require a caller-specific inline-command delivery signal and a real adapter bypass regression before claiming the full slash/model post-delivery barrier. R2 pending parent correction.

## Targeted R2 correction — closed

Reviewed `a71449a566d76061e80438fb0f134006f06008a1`. Busy inline commands now receive an internal per-event delivery signal, completed in the adapter's `finally` after its own reply attempt. The real slash handler forwards that signal separately from the existing generation-bound active-turn barrier; `request_restart` awaits both. Authorization, audit refusal, ordinary active-work draining and model-request behavior remain unchanged. Independently reran the isolated restart file: all 5 tests passed, including real `handle_message` busy bypass plus real `_handle_restart_command`. R2 is closed. No unresolved concrete source blocker remains from this bounded review.

## Draft ledger check

Programmatic comparison found the exact historical set of 51 IDs (37 old_main and 14 archive_only), all 239 original CSV evidence objects retained, and 241 subfeature entries including two explicit added distinctions. All subfeature dispositions use the allowed vocabulary; owning hashes resolve to commits and referenced runtime test files exist. Flagged draft accuracy corrections: FC28-E05 must cite the TTS test instead of the dependency test; FC36 retained audit-denial and intentional interface rewrite require distinct dispositions, and untested all_profiles preflight must not inherit a blanket executable restoration claim. Parent is correcting these before final documentation review.

## Final independent disposition

Final reviewed HEAD: `e36f3d76ff7464cf51a0a5881934e005fbbb9ee3` (ledger/docs). Final production source candidate: `a71449a566d76061e80438fb0f134006f06008a1`. Both identified acceptance blockers R1/R2 are closed by separately reviewed, independently rerun regression fixes. **No unresolved concrete acceptance blocker found in this bounded source-and-handoff review.** This is not a source/CI-ready, full-parity or live-readiness attestation.

Rechecked committed documentation and ledger: all 51 expected row IDs, exact 37/14 scope split, all 239 original historical evidence objects, valid dispositions, existing referenced tests and resolvable full owning commit hashes. The FC28 display-test reference and FC36 retained/policy/unqualified distinctions are corrected. The human handoff explicitly records partial restoration, unresolved executable qualification, approval gates and exclusion boundaries. Final `git diff --check cf62291dad..HEAD` passed and working tree status was clean. No reviewer source, test, index or commit mutations occurred.

Independent execution totals: initial 15 passing tests across cleanup/cron/restart/process-tree files; targeted correction runs of 4 then 5 restart tests (repeated cases, not 24 unique tests). The new stalled-callback and busy-slash cases are additional independently passing regressions. Parent's broader qualification and added-line security receipt remain separate implementer evidence. No further audit cycle requested: Ang should independently verify/integrate the exact candidate and run approved hosted pytest/CI, while Brian decides the documented critical slices.
