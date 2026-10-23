# Reliable delivery qualification

Current evidence for [#27](https://github.com/brianfaires/hermes-agent/issues/27),
following the [baseline](software-delivery-baseline-2026-10-22.md). This record
separates source qualification from installed delivery. The installed owner-chain
acceptance in #35 remains required.

| Slice | Repair and observed result |
| --- | --- |
| #29 continuation | Restore the retained continuation adapter against current defining-module APIs. Honor native effect receipts, profile ownership, current holds and grants. A shared script/monitor explicit `wakeAgent` gate remains retryable until its recipient acts, rather than consuming a hash before inference succeeds. Native integration passes for both default and served coordinator profiles: omitted notification, actual successor process, interrupted recipient retry, pause, script failure, unchanged quiet checks, no initiating transcript growth. |
| #30 capacity | Existing dispatcher behavior satisfies the combined case. A real specialist process parks on a genuine dependency; unrelated eligible work starts; a manual hold and dependency remain effective. Resolving the prerequisite resumes the original task with a new run; stale completion and repeated dispatch cannot claim it twice. No capacity implementation added. |
| #31 recovery | Interactive recovery now reconciles recorded results and actual effects before continuing the first unfinished authorized step. New instructions take priority; restart commands stay protected from replay. A bounded real-agent comparison reproduced restore-only abandonment on the base guidance, then completed closeout with the repair while leaving the mutation count at one. A newer cancellation left closeout absent. |
| #32 selected launch | Native dispatch resolves role-required skills in the selected profile, including review's `sdlc-review`. Archived optional pins are omitted only from the launch copy; their original task metadata remains intact and a native worker session completes. Assignments with enabled terminal tools receive a bounded harmless command through their configured backend, including scratch workspaces. The probe binds the worker's workspace mount context and uses its own disposable environment. Deterministic missing prerequisites park once with the underlying owner action; corrected prerequisites allow actual execution while retaining the candidate. Transient execution errors retain bounded retry. An executor `TypeError` after a recorded effect no longer invokes the spawn callback a second time. |
| #33 release | The existing owner-managed kit survives executor exit after stopping a disposable native service. It restarts qualified source, preserves newer unrelated holds, and refuses unsafe source. The observed missing `RESULT.md` is repaired in the kit's existing fallback; successful and recovery-required outcomes are readable independently of the gateway. No replacement release machinery added. |
| #34 scope/evidence | Declared local artifact contents, including files outside the scratch directory, are copied into task attachments before native scratch cleanup. Already durable handoff files are reused across review. Missing required evidence prevents completion without losing the workspace. A bounded real agent reads a compact work order and actual artifact, accepts the original outcome despite an unrelated optional discovery, and rejects unavailable required evidence. No universal evidence bundle added. |

The source restoration reuses `ffd4fcb841f60998decc392ba70065f47a9c3749`.
Migrations remove obsolete fixed provider/model gates and retired schema/API
references; current cron/provider resolution controls inference. Native SQLite
ISO timestamps normalize before pending-obligation deadline arithmetic. Actual
task/run/effect identity continues to control acknowledgement.

## Verification

All deterministic tests use `scripts/run_tests.sh` and disposable homes, with
the prepared Python 3.14.7 test interpreter selected via `HERMES_PYTHON`.

- Continuation and ordinary monitor run: 131 tests passed in three files.
- Changed continuation parameter cases: 21 passed after table-driven fixture cleanup.
- Connected dispatch/cron/artifact/capability/process-survival checks: 22 passed in seven files.
- Gateway recovery and goal-resume checks: 44 passed in two files.
- Owner release kit: 24 passed in three files, including two tests reproduced
  failing for the missing independent result before its repair.
- Changed standalone source modules pass `ty`; scoped `ruff` passes.
- Independent review found and repaired missing scratch-route validation and
  incorrect probe mount context. Six launch regressions pass, including both
  persistent and isolated Docker planning with a recording external engine that
  executes real harmless workspace commands. The installed Docker daemon itself
  is not under test in those cases.
- `python3 scripts/check --staged` passed all 12 blocking checks before review;
  affected checks are rerun for the review corrections.

The full local suite was attempted once in the isolated worktree. It discovered
5,603 files (approximately 45,907 tests), then hit repeated real-home I/O guard
failures: fixtures reach the shared Git directory beneath the live installation,
despite the source worktree living outside it. Existing ACP assertion failures
also appeared. The owned runner and its eight current test descendants were
stopped without bypassing the guard. This is an incomplete, failing full-suite
attempt; targeted results do not replace exact-candidate hosted CI.

Red reproductions separately establish the missing continuation module,
ISO timestamp arithmetic, hash consumption before recipient acceptance, missing
review skill launch, and duplicate spawn after executor `TypeError`. Fixture
errors encountered while adapting historical tests are not product reproductions.

Native qualification uses the installed release kit's supported test command,
real user systemd services, the real control-socket client/server, actual process
identities and a disposable SQLite-backed runtime. The executor exits 23 after
stop, before start. All disposable units and runtime unit files are removed or
reset after qualification. This establishes independent recovery on this host;
it does not establish production activation or a live feature verdict.

### Required CI follow-up, 2026-10-23

Hosted macOS CI on `f8321292a9` exposed an existing timeout-cleanup defect:
a reaped launcher no longer resolves through `getpgid(pid)`, leaving its owned
process group alive with captured pipes. The existing webhook transform test
reproduces this on Linux by explicitly reaping its real launcher. Retaining the
private group identity established by `process_group=0` lets cleanup signal that
owned group after the launcher exits. The new case fails before the repair;
20 neighboring subprocess/deadline/webhook tests pass afterward, while an
unrelated real process survives. Native macOS CI still qualifies the correction.

Required Linux and Windows jobs also remained queued on organization-specific
32-core runner labels with no assigned runner. This personal public fork uses
standard available hosted runners by default; repository variables
`CI_LINUX_RUNNER`, `CI_WINDOWS_RUNNER` and `CI_WINDOWS_ARM_RUNNER` can opt into
other capacity. OS and architecture coverage remain unchanged. Worker pools
follow the host CPU count, with conservative configurable concurrency for nested
process suites, and the existing heavy Python jobs have enough wall time for
smaller hosts. This repairs the execution route; no test lane is waived.
See [GitHub's standard runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).

The real-agent exercise uses Ang's existing configured model/provider, without
substituting inference. It is a bounded behavior check, not a prompt snapshot or
keyword proxy. It cannot prove the entire installed Sunny/Rook/Ang relay.

### Completed hosted result and baseline comparison

The exact published candidate `966cd1418e8e511981f9bb6d43678c36f01dd615`
failed required CI in staging run
[39920161278](https://github.com/brianfaires/hermes-agent/actions/runs/39920161278).
The two core lanes each passed more than 30,000 tests and reported 29 failures.
Both macOS lanes passed. Windows, installer and upgrade/git failures also remain;
they are not waived or claimed as locally reproduced on Linux.

The 24 core files containing those 58 failures were rerun through the project
runner in independent temporary repositories. Their indexed trees were verified
against baseline `28b5a2fadcd8cce0941d8c9d147741938f539ab3` and published
candidate `966cd1418e8e511981f9bb6d43678c36f01dd615`, avoiding the live Git-directory
guard limitation. Both produced **542 passed, 58 failed, 4 skipped**, with identical
failing node sets: no added or removed failures. This establishes baseline
attribution for those files, not passing CI. The failures span cron fixture
ownership, media handling, configuration snapshots, provider resolution and undo
contracts; production semantics must not be changed merely to satisfy old tests.

Two relevant follow-up gaps were repaired. The worker preflight had treated an
archived guidance pin as a hard requirement; role requirements remain strict,
while optional pins resolve in the selected profile and only resolvable pins
reach the CLI. The real archived-pin worker now completes instead of taking the
existing known-failure path. The strict native artifact scenario also reproduced
a declared outside-workspace file being omitted from attachments; declared local
files now receive the existing capped durable copy, including review handoffs.
Reused durable attachments must still be available before review can complete; a missing file refuses acceptance, keeps the workspace and removes only new staged copies. External metadata references retain their existing flexible contract.

A JS process-contract test also completed its admission/rejection checks after
roughly 22–28 seconds, exceeding its old 15-second bound. The same timeout was
reproduced against the baseline. Its individual bound is now 60 seconds; the
assertions and workload are unchanged. Focused Vitest and lint pass.

The final follow-up still requires review and exact-candidate CI. The unrelated
baseline failures are a release blocker, not a reason to expand #27 silently or
claim installed acceptance.

## Durable handoff

Resolve these locators beneath the installation's default data root:

- `profiles/ang/artifacts/reliable-delivery-20261022/CURRENT.md`: compact scope,
  authority, phase, owners, next action and current installed qualification state.
- The same directory contains `native-release-receipts.json`,
  `agent-acceptance-receipts.json`, the executable qualification scripts, and the
  exact tested release-kit snapshot. These are evidence, not an execution queue.
- The owner kit remains
  `profiles/ang/skills/repository/release-execution/scripts/`; its repaired fallback
  writes the outcome to each release's existing `RESULT.md`.

Only the previously failing Rook continuation job `a817a0e45f42` was temporarily
paused for compatibility qualification. No live Kanban card was reopened, and no
production gateway stop, source switch, deployment or fault injection was performed
for the checks recorded here. Reconcile and remove only this owned pause when the
installed monitor and current scope qualify. Existing unrelated stops remain binding.

### Required-CI repair follow-up

The final-source PR run 39981255708 completed red. The same source lane then
repaired the reproduced baseline fixture failures instead of waiving them:
cron unit fixtures now stub the receipt write alongside their already mocked
execution bookkeeping; external-worker integration uses actual fenced adoption
and retains real foreign/unadopted receipt refusal coverage. Redaction patches
the called output sibling. Media fixtures request attachments on complete lines,
as the existing standalone-control implementation and documentation require;
prose examples remain literal. The regenerated prompt fixtures reflect that
already-existing guidance. Provider, profile mutation, systemd manager-read and
TUI module-identity fixtures retain their original behavior assertions.

One actual config integration defect needed a production repair: saving consent
advanced the config file without updating a wizard caller's validated revision.
Consent persistence now checks that caller under the existing mutation lock and
refreshes its revision before releasing the lock, preserving pending edits and
rejecting a genuinely stale caller before any consent write. Both regressions
were demonstrated red; the affected consent/config transaction files pass 28
cases. This does not change the consent choices or sending policy.

All 24 previously failing core files now pass in an independent temporary Git
root: 601 passed, zero failed, four skipped. Additional real-ledger, adoption and
redaction verification passes 122 cases; these totals overlap and are not added.
The affected media contracts pass 251 cases. Desktop settings passes 19 cases
after timer-aware notification teardown. A long native Windows help-probe case
has its own file so the outcome file can finish and report assertion failures
within the unchanged per-file deadline; Linux collection is not native proof.

The fork's history contains the old release commits, but its hosted checkout has
no CalVer tags. Upgrade fixture setup now fetches genuine published upstream
release refs into the disposable CI checkout without forced rewrites. This is
fixture metadata preparation, not a source-history or published-tag change.
Hosted required CI on the new exact head and installed acceptance remain gates.

The native Windows Git self-heal failures also exposed a concrete lookup gap:
legacy Program Files and profile/shared-home locations omitted the selected
PM tool store. Recovery now asks `hermes_platform` for the selected Git location
and probes its existing architecture-specific `git-core` layout before the
legacy fallbacks. The resolver performs no execution; the updater still requires
an existing candidate and an explicitly successful non-trampoline probe. Both
x64 and ARM metadata cases failed before this repair and pass afterward. The
focused updater/resolution files pass 12 cases with three Windows-only skips on
Linux. The new resolver typechecks; the legacy updater has the same seven type
diagnostics on baseline and candidate. Native execution remains a hosted gate.

The native release-timestamp fixture also no longer bets that a cold handoff
reaches its release wait within a 25-second lock lifetime. The lock holder
acknowledges acquisition, stays held until the handoff reports entering that
wait, and releases after crossing the receipt timestamp's whole-second boundary.
The assertion compares the final receipt against the observed release time.
This strengthens the same native contract; it remains unqualified until the
Windows jobs execute it.

Archived Windows installer diagnostics identify disk exhaustion in five machines,
including failures after successful dependency-sync receipts. The lane ran six
complete machine journeys concurrently and retained their installed profiles.
Its standard-runner default is now one file at a time, with a repository runner
capacity override. After exporting diagnostics outside its uniquely created
profile, the fixture removes that profile through the existing PM filesystem
helper. Preexisting profiles are refused before staging; local inspection and
in-profile evidence remain available. Both ownership/cleanup regressions were
red before the fix; four filesystem cases pass, including evidence and unrelated
profile preservation. The production installer and upgrade assertions are
unchanged. A fresh native run remains required to establish capacity and behavior.

The next exact-head hosted JS run passed all desktop, TUI and web suites but
failed the staged-source Git integration fixture on Vitest's default five-second
deadline. The unchanged fixture reproduces locally: 10.6 seconds for real Git
setup and cold Python source-check processes, with no failed behavior assertion.
Only this fixture receives a bounded 30-second deadline; its real subprocesses,
staged target, isolated Git configuration, foreign-root refusal and unrelated
invocation assertions are retained. This is qualification infrastructure, not
a product timeout or a waiver of required hosted CI.

That hosted Windows x64 run reports 695 passed and zero assertion failures, but
its commit-outcome file reaches the unchanged 300-second subprocess limit during
the final CPU-busy watchdog case. The earlier R6 synchronization regression now
passes natively. Keep every existing native case and deadline, and group
post-commit results, R6 release waiting, and delegate/watchdog supervision into
separate files so cold launch costs do not accumulate into one file timeout.
Linux collection does not establish native execution of the new grouping.
