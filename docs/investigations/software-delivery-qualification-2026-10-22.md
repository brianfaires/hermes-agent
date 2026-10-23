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
| #32 selected launch | Native dispatch resolves required skills in the selected profile, including review's `sdlc-review`. Assignments with enabled terminal tools receive a bounded harmless command through their configured backend, including scratch workspaces. The probe binds the worker's workspace mount context and uses its own disposable environment. Deterministic missing prerequisites park once with the underlying owner action; corrected prerequisites allow actual execution while retaining the candidate. Transient execution errors retain bounded retry. An executor `TypeError` after a recorded effect no longer invokes the spawn callback a second time. |
| #33 release | The existing owner-managed kit survives executor exit after stopping a disposable native service. It restarts qualified source, preserves newer unrelated holds, and refuses unsafe source. The observed missing `RESULT.md` is repaired in the kit's existing fallback; successful and recovery-required outcomes are readable independently of the gateway. No replacement release machinery added. |
| #34 scope/evidence | Declared artifact contents survive native scratch cleanup. Missing required evidence prevents completion without losing the workspace. A bounded real agent reads a compact work order and actual artifact, accepts the original outcome despite an unrelated optional discovery, and rejects unavailable required evidence. No universal evidence bundle added. |

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

The real-agent exercise uses Ang's existing configured model/provider, without
substituting inference. It is a bounded behavior check, not a prompt snapshot or
keyword proxy. It cannot prove the entire installed Sunny/Rook/Ang relay.

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
