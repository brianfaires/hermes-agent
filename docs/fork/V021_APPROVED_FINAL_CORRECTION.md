# Final finite CI compatibility correction

Corrected source candidate `3604efe12408fc50157e4e0e40198ebe06e8387c`, tree `e3a8a3612eee5c7b2cec842b7da8c084f271abee`, based on preserved `7581ed1325074e7e979d2f0fb00d74dd9ebfa464`. All approved feature scope remains implemented: no pending restoration IDs; the unchanged cumulative checklist covers78 landed IDs, all human results pending. Critical classifications and voice/retired/archive exclusions remain. This correction adds no feature or UAT gate.

## Actual failures and outcome

Hosted run34547488980 at75a30e9809eb2db9edd40fb741c6a43d717c8fb8 **completed FAILURE**:26 unique cases in six Python shards, plus aggregate. Read-only `gh run view 34547488980 --repo brianfaires/hermes-agent --json status,conclusion,headSha,jobs` confirms all other completed nonskipped jobs succeeded. This supersedes previous starting/queued claims. First-batch34545497788 failure remains historical. No full green CI is asserted; Ang must independently verify, integrate/push staging and require exact-candidate full hosted CI. Workers do not push or mutate staging/main/live.

**544 unique pytest cases passed across41 complete files**, including all21 new/edited batch3 files and all26 indexed hosted failure cases. Latest complete-file result replaces earlier repeated runs; subtests and targeted reruns are not added to this count. Exact commands, returns, outputs, file selections and case mapping are embedded in V021_APPROVED_FINAL_CORRECTION_TESTS.json. Earlier red results and timeouts remain preserved with their actual returns.

Two genuine product defects were corrected: profile refusal had lost contextual warning diagnostics; current release observation unconditionally required a new scheduler API unavailable in its already admitted rollback runtime. Gateway still rejects missing/invalid named homes. Release observer prefers current profile-qualified keys, uses the historical locked global-ID snapshot only when the current API is absent, validates snapshots, and refuses getter errors. It never turns unknown work into zero. No historical scheduler source is modified, and legacy counts do not claim current profile-qualified identity equivalence.

Other failures were traced to specific fixture contracts: missing real receiver/profile homes; obsolete global dotenv/cache hook expectations instead of private fresh values; unconnected adapters; unrestricted MagicMocks inventing stale transport provenance; config fixtures disconnected from pinned launch homes; Discord adapter home patched too late; pytest View stub missing lifecycle APIs; non-package compression helper import. Corrected fixtures retain delivery/FIFO/MCP/preview/retry/verbose/auth/preservation assertions and strengthen actual secret rotation, ownership and cleanup checks.

Release qualification exposed additional harness assumptions. Each disposable launch now owns its closed dependency inventory, with actual verification and explicit drift rejection retained. The hosted log identifies an inventory mismatch but not its differing artifact; no more specific cause is claimed. Long-home sockets use a held directory-descriptor alias with a verified physical task-local target, real pointer roundtrip and cleanup. Generation rejection uses its own finite age window solely to satisfy that negative test's freshness precondition; default90-second freshness, stale-marker rejection and31-second producer check remain. The historical rollback case actually runs locally; it was not newly skipped. Fault injection holds a task-owned exclusive file lock while the real heartbeat writer takes a shared lock and still calls its original implementation. The test restores genuine marker bytes in finally, releases the lock, waits for a newer real producer marker, and requires healthy collection. This prevents the ticker repairing the injected fault during collection without fabricating healthy timestamps or broadening expected errors.

## Isolation and unsuccessful attempts

`python3 ../run-pytest-focused.py source REPORT FILES` uses canonical Python read-only, Ang's task-only test-deps-pinned, and a separate fresh HOME/HERMES_HOME/bundles/TMPDIR per file. No correction-worker installation occurred. No live model/delivery, canonical/config/env/venv changes, service restart or staging/main mutation occurred. Disposable local listeners and real owned child/SQLite paths are test fixtures.

The supplied runner's150-second cap was shorter than the hosted release file's154.12-second run and the local real lifecycle. Original helper bytes are preserved. Only the task helper gained an explicit bounded timeout option (default150,max600), owned subprocess-group cleanup and report-path containment; pytest environment/flags remain. Full release qualification uses600; no test wait/assertion was shortened. A deliberate harmless timeout probe returned124 and verified its owned descendant gone; this is an expected negative harness check, excluded from passing product totals.

Exploratory boundary deviations are recorded honestly: earlier long-root probes created transient `/tmp` sockets via fallback; identified owned children/sockets were cleaned before final isolation correction. Three generated credential receipts initially landed one directory above the task due to relative report paths; their exact bytes were relocated into the workspace and the erroneous files removed. All identified temporary artifacts were removed or relocated; canonical and live state were not changed. Final physical socket ownership has a raw receipt.

Expected warnings remain visible: linked-SQLite fallback, audioop/aiohttp notices and the existing scheduler fixture's unawaited coroutine warning (also reproduced before correction). These are not presented as proof of full-suite success. All focused subprocesses and workers must be quiescent at final handoff.

## Exact regression inventory

- `tests/agent/test_prompt_builder.py`: 69 passed; raw `final-correction-batch3-pytest.json`.
- `tests/agent/test_v021_child_env_namespace.py`: 1 passed; raw `final-correction-batch3-pytest.json`.
- `tests/agent/test_v021_compression_publication.py`: 3 passed; raw `final-correction-batch3-pytest.json`.
- `tests/agent/test_v021_compression_tail_guards.py`: 3 passed; raw `final-correction-batch3-pytest.json`.
- `tests/agent/test_v021_prompt_contracts.py`: 3 passed; raw `final-correction-batch3-pytest.json`.
- `tests/cron/test_v021_ancillary_validation.py`: 1 passed; raw `final-correction-batch3-pytest.json`.
- `tests/gateway/test_discord_format.py`: 7 passed; raw `final-correction-batch3-pytest.json`.
- `tests/gateway/test_discord_free_response.py`: 21 passed; raw `final-correction-batch3-pytest.json`.
- `tests/gateway/test_media_spaced_paths_and_history_dedupe.py`: 7 passed; raw `final-correction-batch3-pytest.json`.
- `tests/gateway/test_media_tag_cleanup.py`: 4 passed; raw `final-correction-batch3-pytest.json`.
- `tests/gateway/test_media_tag_formatting_variants.py`: 4 passed; raw `final-correction-batch3-pytest.json`.
- `tests/gateway/test_media_tag_separator.py`: 2 passed; raw `final-correction-batch3-pytest.json`.
- `tests/gateway/test_v021_compression_adoption.py`: 2 passed; raw `final-correction-batch3-pytest.json`.
- `tests/gateway/test_v021_notification_policy.py`: 12 passed; raw `final-correction-batch3-pytest.json`.
- `tests/gateway/test_v021_served_coverage.py`: 10 passed; raw `final-correction-batch3-pytest.json`.
- `tests/gateway/test_v021_text_compatibility.py`: 8 passed; raw `final-discord-text-qualified.json`.
- `tests/gateway/test_v021_webhook_namespace.py`: 1 passed; raw `final-correction-batch3-pytest.json`.
- `tests/gateway/test_webhook_integration.py`: 4 passed; raw `final-correction-batch3-pytest.json`.
- `tests/hermes_cli/test_v021_workspace_metadata.py`: 5 passed; raw `final-correction-batch3-pytest.json`.
- `tests/tools/test_v021_discord_packaging.py`: 1 passed; raw `final-correction-batch3-pytest.json`.
- `tests/tools/test_v021_security_contracts.py`: 6 passed; raw `final-correction-batch3-pytest.json`.
- `tests/gateway/test_profile_resolution.py`: 13 passed; raw `final-correction-profile-resolution-final.json`.
- `tests/gateway/test_multiplex_busy_input_mode.py`: 17 passed; raw `final-correction-gateway-pytest.json`.
- `tests/gateway/test_queue_consumption.py`: 5 passed; raw `final-correction-gateway-pytest.json`.
- `tests/gateway/test_notice_rendering.py`: 4 passed; raw `final-correction-gateway-pytest.json`.
- `tests/gateway/test_reasoning_command.py`: 8 passed; raw `final-correction-gateway-pytest.json`.
- `tests/gateway/test_run_progress_topics.py`: 28 passed; raw `final-correction-gateway-pytest.json`.
- `tests/gateway/test_multiplex_adapter_registry.py`: 22 passed; raw `final-correction-gateway-pytest.json`.
- `tests/gateway/test_gateway_platform_event_hook.py`: 34 passed; raw `final-correction-gateway-pytest.json`.
- `tests/cron/test_v021_private_credentials.py`: 9 passed; raw `final-correction-isolation-regressions.json`.
- `tests/gateway/test_v021_durable_ownership.py`: 12 passed; raw `final-correction-isolation-regressions.json`.
- `tests/cron/test_execution_ledger.py`: 21 passed; raw `final-correction-isolation-regressions.json`.
- `tests/test_v021_restoration_checklist.py`: 1 passed; raw `final-correction-isolation-regressions.json`.
- `tests/gateway/test_v021_compression_failure.py`: 1 passed; raw `final-correction-isolation-regressions.json`.
- `tests/cron/test_cron_bot_chat_delivery.py`: 17 passed; raw `final-correction-credentials-after.json`.
- `tests/cron/test_cron_no_agent.py`: 18 passed; raw `final-correction-credentials-after2.json`.
- `tests/cron/test_scheduler.py`: 103 passed; raw `final-correction-scheduler-order-final.json`.
- `tests/gateway/test_multiplex_credential_isolation.py`: 4 passed; raw `final-correction-credentials-after.json`.
- `tests/gateway/test_discord_connect.py`: 13 passed; raw `final-discord-qualified.json`.
- `tests/gateway/test_v021_transport_identity.py`: 15 passed; raw `final-discord-qualified.json`.
- `tests/scripts/test_release_actual_path.py`: 25 passed; raw `final-release-full-synchronized.json`.

## Cohesive source history

eae10f0131e0ea44be5f96a635e191856f054f45 test(cron): verify private refresh and receiver ownership contracts
5a1986b8acd5430a22480905f56a7bffa5eeb7ad fix(gateway): retain rejection diagnostics and qualify owned lifecycle fixtures
54b8162265593dba7203b3ff1c6ccfd0166b70f0 test(discord): preserve pinned homes and view lifecycle in pytest fixtures
e37a0cc62bbc73b20267b2a65010ac20d9bb7d2c fix(release): preserve historical cron observation and isolated lifecycle tests
b3fbe551fbcc18e20138476068ff54eea155e289 test(release): synchronize ticker fault injection with real marker writes
3604efe12408fc50157e4e0e40198ebe06e8387c test(cron): separate process startup from job refresh measurement

## Detailed owner contracts and classifications

Owner records retain intermediate conclusions verbatim; their final qualification sections supersede preliminary fixture-only and deadline statements.

---

Original new correction receipt: final-correction-gateway-contract.md

# Finite qualification correction contracts

Preserve launch-owned config and private named profile runtime resolution; missing named homes refuse rather than fall back to ambient. Fixtures must construct real temporary profile homes/configs and register transport/runtime/session identities as production lifecycle does. Preserve routing, busy queue/steer/interrupt, authorization, MCP inclusion, preview length and notice send behavioral pressure. No blanket fixture classification: trace each failure before editing. Compression test shared helper imports must be package-safe under actual pytest with preservation assertions unchanged. No new features, policy relaxations, skips or installs.

## Classification and executed correction

- Missing/invalid named profile cases revealed a production diagnostics regression: fail-closed rejection had removed warning logs. Restored contextual warnings while retaining ProfileRouteRejected and original cause; original warning assertions remain, former fallback assertion now requires refusal.
- Named routing/message/event fixtures now provide real temporary config homes; dispatch requires a valid home instead of a get_profile_dir(None) bypass.
- Busy-mode adapter fixture now models successful connect/disconnect (_running) lifecycle. All queue/steer/interrupt/restart-drain assertions unchanged.
- Queue and public notice sources use real SessionSource instead of a MagicMock manufacturing stale transport provenance. FIFO and exact send assertions unchanged.
- MCP and preview/progress fixtures pin runner launch home to their actual config; all enabled toolsets, preview lengths, retryable edit and verbose argument expectations unchanged.
- Compression shared fixture import uses the tests.agent package; all three preservation assertions execute under pytest.

Raw final full-file gateway results: final-correction-gateway-pytest.json (131 cases across8 files); final-correction-profile-resolution-final.json additionally replaces the profile-resolution run after using a real routing home. All21 changed batch3 test files: final-correction-batch3-pytest.json. These are focused per-file executions, not a full suite. Prior source receipts untouched.

---

Original new correction receipt: final-correction-credentials.md

# Final CI correction: private credentials / cron fixtures

Read CI-75a30e9-CORRECTION.md and indexed raw baseline: four assigned failures target missing real receiver, obsolete global dotenv/cache calls, and old one-fetch-per-home cache behavior. Reproducing complete files on current source before edits; report final-correction-credentials-before.json.

Compatible contract before mutations: preserve approved private fresh scope on every run/entry, no process-global dotenv mutation or cross-home cache invalidation, named bot-chat targets must exist and child receives receiver home/secrets rather than sender. Replace hook-call fixtures with real temporary .env/external-source refresh and subprocess/agent-boundary observations. Cold profile test must demonstrate changed external values on repeated entry and no sibling/process leakage rather than assert stale single-fetch behavior. Named target fixture creates actual destination profile; assert -p and explicit resolved receiver home, excluded sender keys, safe missing-profile failure before spawn. Existing subprocess/provider external effects remain mocked; no live delivery/model. Production change only if runtime reproduction establishes a real defect, not to satisfy obsolete hook expectations. No installs/live/staging/push.

## Reproduction and classification
All four indexed failures reproduced on current source: `final-correction-credentials-before.json` records 4 failed/138 passed across complete files. Original hosted raw matches at ci-75a30e9-slice1.txt:674, slice5.txt:623, slice6.txt:622, slice7.txt:1237; Ang baseline ang-ci75-local.log corroborates. These are specifically demonstrated contract-incompatible fixtures, not a blanket pre-existing-failure claim.

- Bot-chat case created no research profile and expected receiver HERMES_HOME to be absent. Approved routing rejects nonexistent receivers and passes resolved receiver home to preserve custom roots. Fixture now creates real temp destination/config/.env, uses real exists/resolve functions, checks -p and receiver home/token, rejects sender token/process leakage and missing receiver before spawn.
- No-agent case asserted global load_hermes_dotenv call rather than credential effect. Fixture now rotates actual .env twice and runs real safe shell script; in-process private scope sees fresh home channel, child sanitizer deliberately strips destination while custom noncredential value reaches child. Process env and enclosing scope unchanged.
- Scheduler case required global reset/load hook order. Fixture now uses real private source hydration/cache with fake external registry provider and model boundary, verifies two rotated agent-visible keys, dotenv precedence, retained sibling cache (no extra sibling fetch), unchanged process env and restored scope. `_hermes_home` and actual HERMES_HOME fixture now agree.
- Multiplex cold profile case expected a single cached external fetch over two entries. Fake external source now rotates values and both entries assert their own value; sibling/process leakage negatives retained and count correctly proves two refreshes.
- Same scheduler complete-file run also exposed FakeFuture lacking the real `done()` interface, causing an unhandled inactivity watcher exception. Fixture now tracks completion through result()/done(); original heartbeat assertions unchanged.

No production source changes were needed. Developmental after receipt retained two fixture mismatches (intentional script sanitizer, inconsistent test home), corrected without relaxing those runtime contracts. External bot-chat/model/secret registry side effects are faked; shell script subprocess is real and harmless. No receiver CLI startup/real provider or live delivery proof claimed.

## Final result
Committed `eae10f0131e0ea44be5f96a635e191856f054f45` (four test files only), exact tree/blobs/SHA256 in final-correction-credentials-bindings.json. **142 passed** across all four complete files: bot-chat17 +multiplex4 in final-correction-credentials-after.json; no-agent18 +scheduler103 in final-correction-credentials-after2.json. Earlier failures preserved in respective receipts; only latest passing result per file is qualification. Commands used real authorized run-pytest-focused.py source with pinned task-only pytest9.1.1, canonical read-only Python, temp homes. Reporter resolves names relative to workspace; initial ../report arguments placed these three generated reports one directory too high. Relocated only those newly generated reports into task workspace without changing contents; no residual report outside task workspace. Correct future invocation uses bare report filename.
Owned diff check passed. No production source modifications or skipped/weakened assertions. Audioop deprecation remains; coroutine _send_to_platform never-awaited warning also occurred in before and after complete scheduler runs and was not modified in this bounded credential correction. FakeFuture inactivity thread exception is corrected. No live model/delivery/config/env/venv/staging/push changes. All subprocesses exited; no children. Independent reviewer is separate; quiescent. Exact candidate hosted CI remains required; no green hosted result claimed.

## Same-review targeted cold-import correction

Independent isolated-node pytest found one additional setup hydration: resolving patch("run_agent.AIAgent") imported run_agent and loaded dotenv after the measured fake source was installed. The complete-file run had warmed that module. Explicitly import run_agent before home/source measurement and use patch.object. Exact sibling plus two-run source calls, rotated values, dotenv precedence and process/scope negatives remain unchanged. Raw final-correction-scheduler-order-final.json records isolated node1 passed and complete scheduler103 passed; original independent failure remains immutable. Commit 3604efe12408fc50157e4e0e40198ebe06e8387c.

---

Original new correction receipt: final-correction-release-discord.md

# Bounded CI correction contracts (before edits)

Read CI-75a30e9-CORRECTION.md completely, indexed slice7/slice8 failures and Ang local reproduction (release timeout124 after FF...). No assumption all failures are old or fixture-only.

Discord: pin test adapter home before construction, then deliberately switch ambient home; timeout must clear success metadata in the pinned store and next reconnect must restore actual fingerprint success. Shared pytest UI stub must implement real stop/is_finished lifecycle; retain all six authorization checks and assert finished state on cleanup rather than suppressing missing stop.

Release: retain real source/dependency manifest verification, startup readiness, profile transport/ticker/state/drain observations and explicit drift rejection. No live service or process ownership modification. Only credential-free disposable fake transports, bounded directly-owned test child cleanup, real manifest paths under test temporary homes. Diagnose timeout/long socket paths and stale module manifest before running full file. Per-launch disposable inventory must represent that launch's actual reviewed dependency snapshot; never bypass isolate/drift verification. Preserve existing rollback and actual lifecycle assertions unless parent identifies it outside task scope. No installation, source push/staging/main or live config/state mutation.

---

Original new correction receipt: final-correction-release-discord-evidence.md

# Bounded hosted-CI correction: release fixture and Discord lifecycle

Scope: indexed CI75a30e9 cases in tests/scripts/test_release_actual_path.py, tests/gateway/test_discord_connect.py and tests/gateway/test_v021_transport_identity.py; minimal shared tests/gateway/conftest.py lifecycle stub with parent-approved ownership. Pre-mutation compatible contract: final-correction-release-discord.md. No production implementation change in this worker's delta.

## Reproduction and classification

- Hosted run34547488980 slice7 and Ang immutable-staging local receipt show command-sync retry test reads old success metadata after timeout. Actual source pytest reproduces exactly (final-discord-baseline.json):13 collected,1 fails. Fixture constructed adapter BEFORE monkeypatching home, then seeded another home's state file. With approved durable construction-home identity, the correct production path cannot update that wrong file. Correction pins home before construction, deliberately switches ambient home afterward, and retains every fingerprint/timeout/retry assertion plus verifies no foreign state file was created.
- Hosted slice7 and Ang local receipt show two view tests failing on missing stop() after their authorization assertions. Actual source pytest reproduces (same baseline receipt):15collected,2fail. Canonical unittest had real discord.py View; pytest uses shared _FakeView lacking lifecycle methods. Correction adds only stop()/is_finished() and a stopped flag to existing fake, then keeps mandatory stop calls and adds explicit finished-state assertions for the actual sent approval and all six controls. No authorization assertion skipped or weakened.
- Hosted slice8 reports release preflight **dependency bytes drift**, correctly refused; the raw log does not identify which dependency artifact changed, so no claim is made about the specific changed file. Its entire file took154.12s (14pass,1fail,1existing rollback skip), exceeding the provided local helper's150s whole-file bound. Ang's124 timeout after FF... does not establish a production hang or success. Source isolated health node passes in96.50s before fixture changes (final-release-health-baseline.json), so this failure is not blanket-labelled pre-existing or a permanently broken production path.
- Release dependency manifest was module-scoped while individual disposable launches and unrelated tests had no immutable dependency hold between them. Each launch now gets a fresh actual snapshot through the same production dependency_files and isolate validation; explicit changed-byte rejection tests remain. This repairs fixture ownership of the snapshot; it does not bypass drift checks or establish the unknown historical artifact's identity.
- Local temporary roots are longer than the helper test's direct UNIX-path assertion permits. Production already has a short hashed fallback plus pointer, so the test now asserts the production-resolved path limit. A new actual owned control-socket roundtrip test forces a long home, reads the real pointer, sends a status request through the release consumer and verifies both pointer/socket cleanup. It does not merely relax the length assertion.

## Verification boundaries

Exact provided ../run-pytest-focused.py used, canonical interpreter read-only and authorized task-local test-deps-pinned. Per-node invocations all remain within one source test file and150s deadlines. No further dependency installation. Release fixture creates a disposable git clone and credential-free loopback transport, blocks nonlocal network, and terminates/waits only its directly owned subprocess in finally. No systemd restart, live profile/config/state mutation or actual killer command. Existing historical rollback test is preserved, not newly skipped; node outcomes are recorded honestly below.

Current result receipts:
- final-discord-qualified.json:13connect+15transport passed,6subtests.
- final-discord-text-qualified.json:8text compatibility passed,11subtests, qualifying shared fake lifecycle against current text tests.
- final-release-path-qualified.json:2passed (unique-root resolver contract and actual long-home pointer roundtrip/cleanup).
- final-release-qualified.json: remaining nodes are recorded incrementally; final counts appended after completion.

First final release lifecycle clone was explicitly observed to be based on committed production5a1986b8acd5430a22480905f56a7bffa5eeb7ad (fixture's parent commit). Parent's small gateway diagnostics correction was therefore present, rather than silently testing an earlier source.

## Final isolation and timeout qualification (supersedes preliminary verification wording above)

The initial 150-second per-node attempt did not qualify lifecycle: `final-release-qualified.json` records lifecycle return124, followed by1 compilation and2 explicit startup/dependency drift cases passed. The helper was stopped during the next rollback invocation; that interruption is not a result. The original helper timed out pytest without reaping its launcher. Exact argv-bound child3251771 and its owned `/tmp/hermes-gw-642060f846537d5a.sock` were inspected, terminated, and confirmed absent. The already-started rollback pytest3258011 was interrupted so its fixture reaped launcher3259041; both were confirmed absent. No broad process killer or service action was used.

Earlier baseline-health and pointer tests transiently used production hashed sockets under `/tmp`; cleanup completed. This was outside the requested physical task-local artifact boundary and was caught before final qualification. The final Linux-only fixture holds an O_DIRECTORY descriptor for its owned task directory, sets child TMPDIR to the short `/proc/<pytest-pid>/fd/<descriptor>` alias, and keeps that descriptor open through child termination/wait. The real pointer roundtrip uses the same held alias as tempfile.tempdir and proves the resolved socket's physical parent equals its task temp directory. No symlink or production resolver change. `final-release-proc-path-qualified.json` independently records2 passed with this final fixture. The disposable gateway lifecycle additionally checks any published socket pointer resolves below its task root.

Parent preserved the original helper as `final-correction-runner-original.py`, then added optional bounded `--timeout-seconds` (default150, maximum600) and cleanup of only the directly allocated pytest process group on timeout. Parent's task-only cleanup probe verified a deliberately timed-out pytest and its nested child exited. Final release invocation uses that corrected helper with600 seconds; no readiness, freshness, drift, health or assertion was weakened, and the real31-second freshness wait remains. Command: `python3 ../run-pytest-focused.py source final-release-full-qualified.json tests/scripts/test_release_actual_path.py --timeout-seconds 600`. Final outcome follows below. The original timeouts and earlier receipts remain immutable; none counts as a pass.


First complete final-fixture attempt (`final-release-full-qualified.json`) completed339.31s:15passed2failed, zero timeout. The historical rollback object9ccb53e3 is available locally so its existing availability gate did not skip it (hosted shallow checkout had skipped it). Candidate lifecycle progressed through actual drain/work/freshness checks, then the generation-negative marker hit the earlier90s staleness guard because runtime already exceeded90s. Corrected only that negative call's precondition: `collect` defaults to90 unchanged everywhere else; generation-negative uses finite max(90, elapsed since actual startup +30), which makes actual started-1 deliberately recent but still before startup. It must still raise the exact predates refusal. The separate1000-second stale marker and real31-second wait remain. This is not a production freshness change. Rollback initial health returned opaque Refusal; added failure-only direct owned control status and fixture log diagnostics before selecting any correction. Raw complete failure remains preserved.

Discord coherent correction committed `54b8162265593dba7203b3ff1c6ccfd0166b70f0`, tree`e3158c6c60d6adb5240f1b3140784095baa469d2`;3file hashes in final-discord-correction-bindings.json. Final source fixture production delta remains none for this Discord group.

## Approved narrow release observer compatibility correction

Parent authorized fixing the observer's existing admitted historical rollback contract after the real9ccb53e3 launch exposed missing get_running_job_keys (final-release-rollback-diagnostic.json). Current observer must use keys whenever present, preserving distinct same-ID/profile work counts; only genuinely absent keys permits legacy locked get_running_job_ids. Nonzero legacy aggregate always blocks; no claim of profile-count equivalence. Getter errors, noncallable attributes, missing both and malformed return shapes must refuse, never yield zero or fall back. No scheduler/historical source changes. Add direct observer-path contracts plus real old rollback busy/drain qualification, commit source before final fullfile run.

Release correction committed `e37a0cc62bbc73b20267b2a65010ac20d9bb7d2c`, tree`371b31bb1cdad8fb1bd26db646ffb4570478cb7b`;2file hashes in final-release-correction-bindings.json. Unlike earlier fixture-only stages, this commit intentionally includes the bounded production observer correction approved above. Direct helper runtime tests8passed in final-release-cron-compatibility.json. Fullfile final-release-full-corrected.json launched after commit so disposable candidate clone contains the final production source, with no source edits during run.

Second complete attempt final-release-full-corrected.json completed299.32s:24passed1failed. Historical rollback actually passed its real profile,cron-busy,drain,staleness/transport path after observer compatibility. Candidate generation-negative did not raise because the real ticker can overwrite the injected timestamp while health performs expensive source/socket observations. No result is treated as green; this exposed a test producer/read race requiring deterministic test-owned synchronization. No production age or generation validation changed.

Final test race correction contract (parent approved): only disposable support wraps original per-profile ticker marker producer with shared flock on that same cron store's task-owned .fixture-marker-write.lock. Fault injection takes exclusive lock across both generation/stale negatives, preserving original producer bytes and restoring them in finally before releasing. No observed-health monkeypatch, no assertion retry, no scheduler production change. After releasing, wait for a genuine marker advance and real healthy collect before the existing transport-disconnect negative. All actual positive producer/lifecycle and31s freshness checks remain.

Final synchronized-fixture commit`b3fbe551fbcc18e20138476068ff54eea155e289`, tree`eff433dc7a03375436f293b6a204639977851eed`; all6current worker paths hashed in final-release-discord-final-bindings.json, historical intermediate bindings preserved. Final full25case invocation: `python3 ../run-pytest-focused.py source final-release-full-synchronized.json tests/scripts/test_release_actual_path.py --timeout-seconds 600`.

## Final completed qualification and handoff

`final-release-full-synchronized.json`: **25 passed,0failed,0skipped,327.07s**, actual full source test file with explicit600-second test harness deadline. Both candidate and historical9ccb53e3 rollback executed and passed; hosted historical skip is not reused locally. Real health entrypoint, current source/manifests, deliberate dependency drift rejection, long-home pointer/socket roundtrip, real producer freshness, API/cron/background busy rejection, drain, marker generation/staleness with synchronized corruptions, writer resume, and disconnected transport rejection remain exercised. Two warnings: external discord audioop deprecation and existing aiohttp AppKey suggestion; no new install or warning suppression.

Final local unique worker cases: **61 =25release +13Discordconnect +15transportidentity +8textcompatibility**. Earlier targeted8observer and2proc-path checks overlap the final25 and are not added to the unique total. Discord reports include6transport and11text subtests, reported separately from test-case counts. Baseline/timeout/intermediate failures remain immutable and are not counted as successes.

Final source correction sequence:54b8162265 (Discord fixtures),e37a0cc62b (release observer compatibility and test ownership),b3fbe551fb (test-only deterministic ticker fault injection). The final candidate commit/tree and all6worker path hashes are in final-release-discord-final-bindings.json; intermediate bindings preserved. Production delta is solely the release observer getter compatibility, with current keyed behavior preferred and failures closed. No scheduler/historical source edits, no live changes, no push or staging/main mutation. Ang owns authorized staging integration and exact-candidate hosted CI remains required; this local result is not full hosted CI green.

`final-release-discord-quiescence.json` verifies no owned release launcher remains, final test root removed, known prior transient orphan socket gone, and identified earlier/current owned child PIDs absent. All tool sessions completed. No children delegated by this worker. Source frozen for one independent blocker review; parent handles consolidated evidence/review and docs.


## Independent review and final binding

One independent targeted review covers this correction delta, exact changed source/test paths and the task helper; see V021_APPROVED_FINAL_CORRECTION_REVIEW.md. Prior13 source receipts and the entire feature-row/checklist contents remain unchanged. Final documentation commit/tree and clean/quiescent status are bound in external approved-final-correction-evidence.md after commit, avoiding a self-referential source hash.

Independent review accepted final source3604efe12408fc50157e4e0e40198ebe06e8387c/tree e3a8a3612eee5c7b2cec842b7da8c084f271abee:32 unique cases plus6subtests ultimately passed. Its one cold-import fixture blocker was fixed without weakening assertions and rechecked within the same review. All20 exact path bindings match; no remaining blocker.
