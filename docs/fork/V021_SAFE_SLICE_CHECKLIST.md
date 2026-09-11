# Cumulative Restoration Checklist

Check a box after an observed pass; add `Evidence: your-note-or-link`. Report failures by case + letter (e.g. **7c**); leave them unchecked.

Pick any case once its setup is ready. **Ang prepares synthetic fixtures and fault simulations; you inspect the result.** No need to build test machinery yourself.

**Setup gate:** approved disposable environment only. Activation, restart and live delivery need separate approval. Production stays unchanged; Discord voice is excluded.

<details>
<summary>Shared candidate and rollback boundaries</summary>

Code under test: `7495459929e20d212db631b96559bda12ad18536`; tree: `a8eed568e804af285c3fb2617a0535022e9a5715`. Its passing CI does not certify a later documentation commit. All human checks start pending.

Record tested commit, tree, source checkout, environment, operator/date and evidence location for every checked substep.

Brian approved remaining source/tests/staging restoration on 2026-09-10; human testing is deferred and does not gate development. Critical risk classifications remain in [critical-gates.md](critical-gates.md). Automated inventory and receipts live in the ledger and qualification documents.

Production is unchanged. Activation, restart and live delivery require separate authorization. Ang may integrate and push staging; Brian remains separate human test authority. Retired consumers and archive-only new features stay excluded.

Rollback is not authorized here. Use the ledger's owning commits and keep dependent code/tests together, including registry/release-observer compatibility, restart delivery/audit/drain fixes, credential/transport/ownership changes, compression publication/tail preservation, and manifest/lockfile packaging.

Earlier checkpoint `38c54fbb1b733e6c98e93f9e03f3332f8f5e974f` and historical CI remain historical only. The exact final SHA CI receipt qualifies the staging candidate; no human pass is claimed here.
</details>

## 1. TTS Provider Settings
Coverage: `FC-04-E01`, `FC-04-E02`

- [ ] a. Set bounded ElevenLabs voice controls in synthetic config -> normal synthesis uses them.
- [ ] b. Exercise streaming synthesis -> streaming uses configured values.
- [ ] c. Unset the settings -> provider defaults remain in use.
- [ ] d. Simulate unavailable SDK -> response text remains visible.
- [ ] e. Simulate failed synthesis -> response text remains visible.
- [ ] f. Inspect both failure outputs -> no credential appears.

## 2. Cleanup Eligibility
Coverage: `FC-08-E01`

- [ ] a. Preview cleanup on aged wildcard fixtures -> eligible tracked files are listed.
- [ ] b. Run cleanup on aged wildcard fixtures -> eligible tracked files are removed.
- [ ] c. Include fresh files -> fresh files survive.
- [ ] d. Include legacy directories -> legacy directories survive.
- [ ] e. Include symlinks -> symlinks survive.
- [ ] f. Include untracked descendants -> untracked descendants survive.

## 3. Webhook Transform Cleanup
Coverage: `FC-10-E03`, `FC-30-E02`

- [ ] a. Run disposable JSON transform -> success returns JSON.
- [ ] b. Use Ang-assisted timed-out child fixture -> child descendants are gone.
- [ ] c. Use Ang-assisted timed-out grandchild fixture -> grandchild descendants are gone.
- [ ] d. Keep unrelated process fixture alive -> unrelated processes survive.
- [ ] e. Check script-trigger path -> retired script-trigger mode stays disabled.

## 4. Cron Alert Rendering
Coverage: `FC-12-E01`, `FC-12-E02`, `FC-12-E03`

- [ ] a. Produce synthetic warning -> attention marker appears.
- [ ] b. Produce synthetic failure -> attention marker appears.
- [ ] c. Deliver mismatched same-chat thread -> relevant thread diagnostic appears.
- [ ] d. Deliver different platform/chat -> same-thread warning does not appear.
- [ ] e. Inspect destination -> rendering did not reroute delivery.

## 5. File-Backed Cron Prompts
Coverage: `FC-13-E01`, `FC-13-E02`, `FC-13-E03`, `FC-13-E04`, `FC-13-E05`

- [ ] a. Create file-backed job through tool -> effective inline + file prompt is validated.
- [ ] b. Create file-backed job through API -> effective inline + file prompt is validated.
- [ ] c. Edit prompt file before disposable run -> file content reloads.
- [ ] d. Update through tool, then API -> both validate the effective prompt before saving.
- [ ] e. Edit file to unsafe content before a run -> execution-time validation refuses it.
- [ ] f. Combine unsafe file content with skills -> job is rejected.
- [ ] g. Use relative path -> stored job is unchanged.
- [ ] h. Use missing file -> stored job is unchanged.
- [ ] i. Use oversized file -> stored job is unchanged.
- [ ] j. Use invalid UTF-8 file -> stored job is unchanged.
- [ ] k. Submit invalid update -> stored job is unchanged.

## 6. Compact Progress And Discord Embeds
Coverage: `FC-14-E03`, `FC-14-E04`, `FC-14-E05`

- [ ] a. Show long path in progress -> bounded meaningful preview appears.
- [ ] b. Show shell prologue in progress -> bounded meaningful preview appears.
- [ ] c. Exercise Discord send fixture -> progress embeds are suppressed.
- [ ] d. Exercise Discord edit fixture -> progress embeds are suppressed.
- [ ] e. Exercise Discord forum fixture -> progress embeds are suppressed.
- [ ] f. Exercise Discord overflow fixture -> progress embeds are suppressed.
- [ ] g. Inspect executed command -> arguments are intact.
- [ ] h. Attempt prohibited destination -> existing channel policy refuses it.

## 7. Profile Cron Registry And Recovery
Coverage: `FC-17-E12`

- [ ] a. Dispatch equal job IDs in two disposable profiles -> each profile runs its own job.
- [ ] b. Release one profile's job -> only that profile releases.
- [ ] c. Simulate stale work -> owning profile reconciles only its job.
- [ ] d. Simulate finished work -> owning profile reconciles only its job.
- [ ] e. Drain both profiles -> global drain counts both.
- [ ] f. Keep live peer in another profile -> peer does not block stale recovery.
- [ ] g. Release stale owner -> live peer is not released or interrupted.
- [ ] h. Check finite one-shot -> live record and exact owner fence remain.

## 8. Discord Channel Policy Precedence
Coverage: `FC-17-E09`

- [ ] a. Load top-level channel settings -> explicit allow/deny wins before connection.
- [ ] b. Load nested channel settings -> explicit allow/deny wins before connection.
- [ ] c. Use explicit empty values -> empty config still wins before connection.
- [ ] d. Compare connected nonempty snapshot -> profile snapshot is authoritative.
- [ ] e. Set conflicting environment -> explicit denial is not bypassed.
- [ ] f. Miss scoped config -> no other profile environment is borrowed.
- [ ] g. Send to ignored channel -> channel still denies.
- [ ] h. Check thread parent -> parent identity is verified.

## 9. Kanban Board Inventory
Coverage: `FC-18-E01`, `FC-18-E02`

- [ ] a. List two disposable boards with one task DB pin -> each row reports its own canonical DB path.
- [ ] b. Inspect board counts -> each row reports its own counts.
- [ ] c. Compare database files before/after -> inventory is read-only.

## 10. Kanban Notification Diagnostics
Coverage: `FC-19-E07`

- [ ] a. Inject notification delivery failure -> operator diagnostics include traceback.
- [ ] b. Inspect destination policy -> no destination changed to recover delivery.
- [ ] c. Inspect subscription policy -> no subscription changed to recover delivery.
- [ ] d. Inspect diagnostic evidence -> no credential is present.

## 11. Delegated Child Lifecycle Isolation
Coverage: `FC-20-E06`, `FC-20-E07`, `FC-20-E08`, `FC-20-E09`, `FC-20-E11`

- [ ] a. Initialize disposable worker task -> only owning parent supplies worker guidance.
- [ ] b. Stop delegated child -> it does not update the parent's task lifecycle.
- [ ] c. Finalize delegated child -> it does not finalize the parent's task.
- [ ] d. Report child activity -> it does not publish parent-task activity.
- [ ] e. Repeat initialization in cron context -> it receives no parent worker guidance.
- [ ] f. Run the owning parent's lifecycle/activity path -> its own task updates normally.
- [ ] g. Attempt child nudge of parent board -> mutation is refused.
- [ ] h. Attempt child finalize of parent board -> mutation is refused.
- [ ] i. Attempt child board mutation -> mutation is refused.
- [ ] j. Check worker-only skills -> child does not acquire them.
- [ ] k. Compare existing conversation prompt bytes -> prompt is unchanged.

## 12. Compression Bookkeeping
Coverage: `FC-25-E05`

- [ ] a. Simulate compression with neither rotation nor in-place publication -> operation reports failure.
- [ ] b. Inspect tokens/bookkeeping -> prior state is retained.
- [ ] c. Inspect status -> no success or committed status is recorded.
Note: published-child rollback is not claimed.

## 13. External Skill Cache And Descriptions
Coverage: `FC-26-E01`, `FC-26-E02`

- [ ] a. Edit external skill then build fresh prompt -> new build reflects edit.
- [ ] b. Add external skill then build fresh prompt -> new build reflects addition.
- [ ] c. Delete external skill then build fresh prompt -> new build reflects deletion.
- [ ] d. Change external skill category then build fresh prompt -> new build reflects category.
- [ ] e. Request bounded descriptions -> requested bound is honored.
- [ ] f. Compare running conversation prompt bytes -> prompt stays stable.
Note: automatic description default is not changed.

## 14. Core Dependency Floors
Coverage: `FC-28-E01`

- [ ] a. Inspect candidate dependency resolution for core paths -> Starlette satisfies restored floor.
- [ ] b. Inspect candidate dependency resolution for core paths -> multipart satisfies restored floor.
- [ ] c. Check voice dependencies -> no voice runtime qualification is inferred.
- [ ] d. Check environment -> no live install occurred.

## 15. Test-Runner Cleanup
Coverage: `FC-32-E01`

- [ ] a. Run harmless disposable test child with oversized BLAS value -> child count is 1.
- [ ] b. Run harmless disposable test child with oversized OMP value -> child count is 1.
- [ ] c. Run harmless disposable test child with oversized MKL value -> child count is 1.
- [ ] d. Run harmless disposable test child with oversized NUMEXPR value -> child count is 1.
- [ ] e. Run harmless disposable test child with oversized VECLIB value -> child count is 1.
- [ ] f. Force spawn failure -> allocated failed-spawn temp root is removed.
- [ ] g. Inspect parent environment -> parent values survive.
- [ ] h. Inspect unrelated temp roots/files -> unrelated paths survive.

## 16. Gateway Restart Delivery Barrier
Coverage: `FC-36-E02`, `FC-36-E03`, `FC-36-E04`, `FC-36-E05`, `FC-36-E06`, `FC-36-E07`

- [ ] a. With separate disposable restart authorization, request model-tool restart -> restart waits for caller delivery.
- [ ] b. With separate disposable restart authorization, request ordinary slash restart -> restart waits for caller delivery.
- [ ] c. With separate disposable restart authorization, request busy-inline slash restart -> restart waits for caller delivery.
- [ ] d. Delay caller response delivery -> barrier holds after active agent count reaches zero.
- [ ] e. Release correct generation delivery -> intended profiles drain and reconnect.
- [ ] f. Try denied request -> restart does not run.
- [ ] g. Try unauditable request -> restart does not run.
- [ ] h. Fire unrelated callback -> barrier is not released.
- [ ] i. Fire stale callback -> barrier is not released.
- [ ] j. Use inert supervisor boundary until authorized -> no live restart occurs.

## 17. Launch And Runtime Identity
Coverage: `FC-17-E03`, `FC-17-E06`, `FC-17-E24`

- [ ] a. Construct gateway/adapter for A, then enter ambient B -> A stays default.
- [ ] b. Submit unrouted fixture message -> A handles by default.
- [ ] c. Submit explicitly routed fixture message -> named runtime handles it.
- [ ] d. Use equal home -> stable cache signature is reused.
- [ ] e. Route to missing named runtime -> request refuses.
- [ ] f. Use differing homes -> cache identity is not shared.
- [ ] g. Inspect busy/session routing -> owner is stamped first.
- [ ] h. Inspect serialized transport data -> transport references are absent.

## 18. Adapter Durable Home
Coverage: `FC-17-E04`

- [ ] a. Construct Discord adapter A, then enter ambient B -> A state paths are used.
- [ ] b. Trigger tracker operation -> only A disposable state is used.
- [ ] c. Trigger command sync -> only A disposable state is used.
- [ ] d. Trigger pairing operation -> only A disposable state is used.
- [ ] e. Inspect B state -> B state is unchanged.
- [ ] f. Inspect pairing grants -> B grants are not read or changed.

## 19. Transport Delivery And Pairing
Coverage: `FC-17-E05`, `FC-17-E20`, `FC-17-E21`, `FC-17-E22`

- [ ] a. Receive through A while runtime routes to B -> delivery authorization follows A.
- [ ] b. Exercise DM policy -> authorization follows A.
- [ ] c. Exercise pairing approval -> authorization follows A.
- [ ] d. Exercise pairing request -> authorization follows A.
- [ ] e. Exercise rate-limit path -> authorization follows A.
- [ ] f. Stop retained transport -> fallback to B is refused.
- [ ] g. Unregister retained transport -> fallback to B is refused.
- [ ] h. Remove secondary store -> default grants or policy are not borrowed.

## 20. Delayed Discord Controls
Coverage: `FC-17-E23`

- [ ] a. Open execution approval from A, then enter B -> A captured home/settings are used.
- [ ] b. Open slash confirmation from A, then enter B -> A captured home/settings are used.
- [ ] c. Open update prompt from A, then enter B -> A captured home/settings are used.
- [ ] d. Open model picker from A, then enter B -> A captured home/settings are used.
- [ ] e. Open choice picker from A, then enter B -> A captured home/settings are used.
- [ ] f. Open clarify choice from A, then enter B -> A captured home/settings are used.
- [ ] g. Enable B grants/allow-all -> B cannot authorize A control.
- [ ] h. Check user restriction -> existing restriction still applies.
- [ ] i. Check role restriction -> existing restriction still applies.
- [ ] j. Check admin restriction -> existing restriction still applies.

## 21. Private Cron Credentials
Coverage: `FC-17-E14`, `FC-17-E15`, `FC-17-E16`

- [ ] a. Rotate disposable per-profile credentials -> each profile refreshes privately.
- [ ] b. Run direct cron jobs -> child environment uses correct profile.
- [ ] c. Run concurrent scripts -> each child environment uses correct profile.
- [ ] d. Run destination-profile bot fixture -> caller scope is restored.
- [ ] e. Inspect process environment -> no mutation occurred.
- [ ] f. Try cross-profile secret borrowing -> request refuses.
- [ ] g. Check script sanitizer -> provider secret cannot escape.
- [ ] h. Use missing destination -> request refuses.
- [ ] i. Use unscoped multiplex -> request refuses.
Note: real mounted-root bot CLI startup remains pending UAT.

## 22. Durable Session Ownership And Resume
Coverage: `FC-17-E25`, `FC-17-E26`, `FC-17-E30`

- [ ] a. Recover owned legacy fixture session -> route and metadata commit together.
- [ ] b. Recover owned current fixture session -> route and metadata commit together.
- [ ] c. Resume owned session -> reopen/reset metadata commits together.
- [ ] d. Resume compression ancestry -> peer metadata commits before memory publication.
- [ ] e. Use foreign target -> rows and route stay unchanged.
- [ ] f. Use missing target -> rows and route stay unchanged.
- [ ] g. Use foreign ancestor -> rows and route stay unchanged.
- [ ] h. Use missing ancestor -> rows and route stay unchanged.
- [ ] i. Inject SQL failure -> rows and route stay unchanged.
- [ ] j. Try ownerless multiplex -> request refuses.
- [ ] k. Replay stale snapshot -> committed data is not overwritten.
- [ ] l. Fail mirror write after DB commit -> committed DB state remains.

## 23. Webhook Profile State
Coverage: `FC-10-E02`

- [ ] a. Deliver identical route/provider IDs in two profiles -> rate state is independent.
- [ ] b. Deliver identical route/provider IDs in two profiles -> idempotency state is independent.
- [ ] c. Deliver identical route/provider IDs in two profiles -> cached destinations are independent.
- [ ] d. Rebind route -> old route's state and cached destination are not reused.
- [ ] e. Retry an unchanged route with the same owner -> retry is duplicate.
- [ ] f. Use wrong profile -> request refuses before consuming state.
- [ ] g. Use wrong HMAC -> request refuses before consuming state.
- [ ] h. Inspect provider ID -> raw provider ID remains intact.
- [ ] i. Resolve route toolsets for current source ID -> toolsets resolve.
- [ ] j. Resolve route toolsets for legacy source ID -> toolsets resolve.

## 24. Cron Ancillary Validation
Coverage: `FC-11-E08`

- [ ] a. Create plain name -> name trims.
- [ ] b. Update plain name -> name trims.
- [ ] c. Create toolset list -> list normalizes and deduplicates.
- [ ] d. Update toolset list -> list normalizes and deduplicates.
- [ ] e. Submit nested value -> write refuses and stored bytes stay unchanged.
- [ ] f. Submit nonstring value -> write refuses and stored bytes stay unchanged.
- [ ] g. Submit invalid value -> write refuses and stored bytes stay unchanged.
- [ ] h. Load untouched legacy record -> record still loads.

## 25. Literal Text And Standalone Media
Coverage: `FC-14-E01`, `FC-14-E02`

- [ ] a. Send Discord prose -> prose stays literal.
- [ ] b. Send Discord table -> table stays literal.
- [ ] c. Send Discord code -> code stays intact.
- [ ] d. Send supported standalone MEDIA directive -> file delivers and directive disappears.
- [ ] e. Send document marker -> document handling is consistent.
- [ ] f. Send real spaced filename -> supported file delivers.
- [ ] g. Put MEDIA-like text in prose -> file does not deliver.
- [ ] h. Put MEDIA-like text in JSON -> file does not deliver.
- [ ] i. Put MEDIA-like text in code -> file does not deliver.
- [ ] j. Put MEDIA-like text in blockquote -> file does not deliver.
- [ ] k. Use glued MEDIA tag -> file does not deliver.
- [ ] l. Use missing file -> file remains protected.
- [ ] m. Use denied file -> file remains protected.
- [ ] n. Remove marker -> inline code examples are not promoted.

## 26. Free-Response Threading
Coverage: `FC-15-E01`

- [ ] a. Enable automatic threading in disposable free-response channel -> response runs in created thread.
- [ ] b. Set explicit no-thread -> response stays inline.
- [ ] c. Disable threading -> response stays inline.
- [ ] d. Fail thread creation -> agent is not invoked inline.
- [ ] e. Fail thread creation -> reply restriction is not bypassed.
- [ ] f. Fail thread creation -> DM restriction is not bypassed.
- [ ] g. Fail thread creation -> channel restriction is not bypassed.

## 27. Served-Profile Ownership And Status
Coverage: `FC-17-E17`, `FC-17-E18`, `FC-17-E29`

- [ ] a. Start successful fixture profile -> launch-owned runtime publishes connected membership.
- [ ] b. Start failed fixture profile -> invalid runtime does not publish connected coverage.
- [ ] c. Start adapterless fixture profile -> invalid runtime does not publish connected coverage.
- [ ] d. Reconnect fixture profile -> connected membership updates.
- [ ] e. Stop fixture profile -> connected membership clears.
- [ ] f. Check CLI status -> only validated connected coverage appears.
- [ ] g. Present stale record -> coverage refuses.
- [ ] h. Present dead record -> coverage refuses.
- [ ] i. Present malformed record -> coverage refuses.
- [ ] j. Present wrong-home record -> coverage refuses.
- [ ] k. Reconnect after shutdown -> membership is not published.
- [ ] l. Check shared routing eligibility -> configured behavior survives.
- [ ] m. Check shared cron eligibility -> configured behavior survives.

## 28. Kanban Workspace Metadata
Coverage: `FC-18-E03`, `FC-18-E04`, `FC-18-E05`

- [ ] a. Create directory metadata through CLI -> normalized complete metadata persists.
- [ ] b. Create directory metadata through API -> normalized complete metadata persists.
- [ ] c. Edit worktree branch metadata through CLI -> normalized complete metadata persists.
- [ ] d. Edit worktree branch metadata through API -> normalized complete metadata persists.
- [ ] e. Edit metadata through DB fixture -> normalized complete metadata persists.
- [ ] f. Use inherited board workdir -> valid inheritance is supported.
- [ ] g. Check dashboard -> branch is forwarded.
- [ ] h. Use scratch branch -> write fails atomically.
- [ ] i. Use invalid ref -> write fails atomically.
- [ ] j. Use invalid path -> write fails atomically.
- [ ] k. Update invalid legacy branch -> write fails atomically.

## 29. Kanban Notification Policy
Coverage: `FC-19-E01`, `FC-19-E02`, `FC-19-E03`, `FC-19-E04`, `FC-19-E05`

- [ ] a. Subscribe through CLI -> one owner-profile resolver selects or refuses destination.
- [ ] b. Subscribe through tool -> one owner-profile resolver selects or refuses destination.
- [ ] c. Subscribe through slash -> one owner-profile resolver selects or refuses destination.
- [ ] d. Subscribe through dashboard -> one owner-profile resolver selects or refuses destination.
- [ ] e. Deliver pending notification -> TUI subscriptions are preserved.
- [ ] f. Deliver pending notification -> policy exemptions are preserved.
- [ ] g. Exercise origin policy -> permitted delivery keeps the originating destination.
- [ ] h. Exercise deny policy -> delivery is refused.
- [ ] i. Exercise Telegram-home policy -> permitted delivery uses the owner's configured home.
- [ ] j. Use missing config -> destination denies.
- [ ] k. Use malformed config -> destination denies.
- [ ] l. Use foreign config -> destination denies.
- [ ] m. Redirect delivery -> origin reply metadata is not retained.
- [ ] n. Redirect delivery -> wake metadata is not retained.
- [ ] o. Fail delivery -> original cursor rewinds and subscription remains.
- [ ] p. Newly deny delivery -> original cursor rewinds and subscription remains.
- [ ] q. Remove existing denied subscription -> removal still works.

## 30. Complete Child Environment Scrub
Coverage: `FC-20-E12`

- [ ] a. Launch harmless delegated child with known worker keys -> all worker keys are absent.
- [ ] b. Launch harmless delegated child with invented worker keys -> all invented worker keys are absent.
- [ ] c. Inspect the child's environment -> no HERMES_KANBAN_* key remains.
- [ ] d. Include unrelated values -> unrelated values survive.
- [ ] e. Inspect parent environment -> parent is intact.
- [ ] f. Attempt direct durable Kanban mutation -> delegated marker refuses it.
- [ ] g. Attempt scratch-board bypass -> no writable bypass exists.

## 31. Atomic Compression Publication
Coverage: `FC-25-E04`

- [ ] a. Partially compress disposable session -> requested tail survives.
- [ ] b. Add newer parent turns during compression -> newer parent rows survive.
- [ ] c. Add newer child turns during compression -> newer child rows survive.
- [ ] d. Publish compression -> publication is atomic.
- [ ] e. Let gateway adopt child -> gateway adopts without rewriting.
- [ ] f. Inject commit failure -> parent is retained.
- [ ] g. Create ambiguous boundary -> compression refuses.
- [ ] h. Grow session past boundary -> compression refuses.
- [ ] i. Run todo/salvage path -> preserved suffix is unchanged.
- [ ] j. Fail later route save -> canonical child remains.
- [ ] k. Fail later route save -> parent is not reopened.

## 32. Prompt Description And Execution Budgets
Coverage: `FC-26-default`, `FC-27-E01`, `FC-27-E02`

- [ ] a. Build fresh prompt with long skill descriptions -> default description budget is 1024.
- [ ] b. Build prompt with explicit bound -> explicit bound is honored.
- [ ] c. Use exact sol variant -> bounded guidance follows configured gates.
- [ ] d. Use exact terra variant -> bounded guidance follows configured gates.
- [ ] e. Use similar model name -> model does not match.
- [ ] f. Use empty tools gate -> guidance stays excluded.
- [ ] g. Use disabled gate -> guidance stays excluded.
- [ ] h. Repeat prompt build -> prompt remains stable.
- [ ] i. Compare existing conversation prompt -> prompt remains stable.

## 33. Pip-Compatible Discord Packaging
Coverage: `FC-28-E02`

- [ ] a. Inspect candidate manifest -> Discord/PyNaCl/Davey pins are present.
- [ ] b. Inspect lazy commands -> packaging pins remain lazy-compatible.
- [ ] c. Resolve explicit requirements with pip in disposable no-install resolver -> pins resolve.
- [ ] d. Resolve explicit requirements with uv in disposable no-install resolver -> pins resolve.
- [ ] e. Compare pip and uv -> both agree on current aiohttp/Brotli.
- [ ] f. Check voice extra -> no incompatible voice extra is implicit.
- [ ] g. Check crypto workaround -> no uv-only workaround is required.
Note: no live install occurred.
Note: parked voice source is not qualified.
Note: unsupported runtime parity is not claimed.

## 34. Test Process Classifier
Coverage: `FC-30-E01`

- [ ] a. Feed executable literals to pure guard -> prohibited process targets block.
- [ ] b. Feed wrapper literals to pure guard -> prohibited process targets block.
- [ ] c. Feed control-flow literals to pure guard -> prohibited process targets block.
- [ ] d. Feed harmless operands to pure guard -> innocent echo/cat text is allowed.
- [ ] e. Confirm fixture safety -> killer strings are never executed.
- [ ] f. Feed env split-string pattern -> bypass fails.
- [ ] g. Feed shell condition pattern -> bypass fails.
- [ ] h. Feed full-python pattern -> bypass fails.
- [ ] i. Check ancestor PID restriction -> restriction remains.
- [ ] j. Check foreign PID restriction -> restriction remains.

## 35. OSV Publication Policy
Coverage: `FC-30-E03`, `FC-30-E04`

- [ ] a. Inspect upstream workflow inputs -> scanning stays enabled.
- [ ] b. Inspect fork workflow inputs -> scanning stays enabled.
- [ ] c. Inspect upstream artifacts -> artifacts stay enabled.
- [ ] d. Inspect fork artifacts -> artifacts stay enabled.
- [ ] e. Evaluate upstream fixture -> code-scanning publication is enabled.
- [ ] f. Evaluate fork fixture -> code-scanning publication is suppressed.
- [ ] g. Confirm fork suppression -> scan itself is not suppressed.
Note: no hosted success is inferred from local checks.

## 36. Tirith Complete Finding Verdict
Coverage: `FC-33-E01`

- [ ] a. Feed exact-package warning -> only justified warning suppresses.
- [ ] b. Feed mixed warnings beyond display cap -> full finding set determines verdict.
- [ ] c. Feed malformed findings -> warning is retained.
- [ ] d. Feed blocking verdicts -> BLOCK and evidence never relax.
- [ ] e. Feed near-match package names -> warning is retained.
- [ ] f. Feed unknown details -> warning is retained.
- [ ] g. Feed nonsuppressible findings -> warning is retained.

## 37. Retained: Delayed Model Picker
Coverage: retained earlier checkpoint

- [ ] a. Select a model for named profile after delay -> only that profile changes.
- [ ] b. Inspect other profile settings -> they remain unchanged.

## 38. Retained: Secondary Cron Coverage
Coverage: retained earlier checkpoint

- [ ] a. Run allowlisted secondary-profile cron fixture -> its heartbeat appears.
- [ ] b. Inspect adapter identity -> secondary profile identity appears.
- [ ] c. Inspect excluded profiles -> they remain inactive.

## 39. Retained: Secondary-Only Notification Transport
Coverage: retained earlier checkpoint

- [ ] a. Use platform connected only for intended secondary profile -> permitted notification delivers through that profile.
- [ ] b. Remove secondary-only route -> default profile fallback does not occur.
- [ ] c. Check notification policy -> policy is not bypassed.

## 40. Retained: Shared Restart Preflight
Coverage: retained earlier checkpoint

- [ ] a. Inspect active work before restart-barrier check -> all active work appears.
- [ ] b. With separate disposable restart authorization, continue barrier check -> restart waits for caller delivery.
- [ ] c. Without separate authorization -> no real restart is executed.

## 41. Retained: Linked-Worktree Setup Safety
Coverage: retained earlier checkpoint

- [ ] a. Exercise setup in disposable linked-worktree fixture -> canonical launcher is unchanged.
- [ ] b. Exercise setup in disposable linked-worktree fixture -> shell configuration is unchanged.
Note: bootstrap is not run against live installation.
Note: repair is not run against live installation.
