# Software delivery baseline

Baseline for [#28](https://github.com/brianfaires/hermes-agent/issues/28), under
[#27](https://github.com/brianfaires/hermes-agent/issues/27), observed on
2026-10-22 against source commit `2aecdc1ae75d6bba9336044617fecbcc9f7256fe`.

The existing Kanban implementation already provides durable wake retries, run
identity, same-card review, artifact preservation, and bounded worker recovery.
Two current differences from the requested delivery contract are identifiable:
the installed continuation adapter requires a module absent from this source,
and interactive restart instructions abandon unfinished work. Passing the
existing component tests does not establish unattended installed delivery.

This baseline changes no runtime code, profiles, jobs, holds, or service state.
It does not authorize replaying historical releases or resuming parked cards.
The execution ledger remains Kanban; this dated record supplies evidence and
follow-on boundaries, not a replacement work queue.

## Findings and owners

“Satisfied” below applies only to the named sub-contract. “Remaining gap” means
a reproducible difference from #27. “Blocked” means the full contract still
needs the specified acceptance exercise; it does not assert an unobserved bug.

| Retrospective finding | Current disposition and evidence | Owner and next ticket |
| --- | --- | --- |
| Dropped continuation | **Remaining gap.** The installed monitor imports `hermes_cli.kanban_continuation`; the module does not resolve in this checkout. Its enabled job records an error. Generic notifier retry and abandoned-claim recovery pass, but do not exercise this installed adapter or subsequent owner execution. | Ang owns source compatibility; Ops owns the existing host adapter; Rook owns acceptance. [#29](https://github.com/brianfaires/hermes-agent/issues/29). |
| Contradictory recovery instructions | **Remaining gap.** Empty interactive recovery explicitly asks what to do next and skips unfinished work. Noninteractive recovery continues the task, and existing recovery lifecycle checks pass. Preserving restart-loop protections while recovering current authorized work needs the connected acceptance case. | Ang. [#31](https://github.com/brianfaires/hermes-agent/issues/31). |
| Incompatible worker execution | **Blocked on selected-route acceptance.** The required `sdlc-review` skill resolves for Ang. It does not resolve for default, Rook, or Ops in the checked catalog. The dispatcher appends that skill at review launch; CLI finalization rejects an entirely missing preload set only after launch. No fresh model/executor or service-capability probe was run. | Ang owns worker/reviewer selection and generic launch behavior; Ops owns actual system prerequisites. [#32](https://github.com/brianfaires/hermes-agent/issues/32). |
| Model-dependent release steps | **Blocked on native supervision qualification.** The existing kit already launches an independent executor with `ExecStopPost`, `RemainAfterExit=no`, and qualified-source recovery. Its existing isolated tests simulate the service manager; they cannot establish native survival after executor exit. This is not evidence that a replacement kit is needed. | Ang, with Ops only for host prerequisites. [#33](https://github.com/brianfaires/hermes-agent/issues/33). |
| Expanding completion requirements | **Blocked on bounded review exercise.** Current release guidance already distinguishes startup health from live acceptance and does not require generic renewed deployment approval. There is no demonstrated need for another approval stage. A reviewer must still show that an optional discovery does not redefine the original outcome. | Rook coordinates scope; Ang owns engineering/release review; Ops owns task-required configuration. [#34](https://github.com/brianfaires/hermes-agent/issues/34). |
| Excessive context and evidence reconstruction | **Satisfied for declared attachment staging and receipt readback; blocked for artifact-content verification and compact owner recovery.** The real-executor handoff test checks durable attachment-file existence after scratch cleanup and repeatedly reads the executor receipt. It does not verify the preserved declared result's contents. Current continuation guidance asks for scoped summaries and deltas. A fresh-owner exercise and rejection of unavailable required evidence remain necessary; the continuation skill's no-chat-platform assumption also needs reconciliation with current routing. | Rook and the existing profile/skill owners; Ang for a demonstrated generic source defect. [#34](https://github.com/brianfaires/hermes-agent/issues/34). |

The related capacity slice remains [#30](https://github.com/brianfaires/hermes-agent/issues/30).
Existing cap and sticky-block tests establish useful primitives, not the combined
case of a parked specialist, a real prerequisite, an unrelated eligible task, and
a manual hold. Do not remove dependency edges merely to make that case pass.

## Concrete reproduction and intent

### Continuation adapter compatibility

The installed owner monitor imports the continuation module before entering its
gate. A read-only module-resolution probe with the prepared Python 3.14 test
interpreter returns no spec for `hermes_cli.kanban_continuation`. Inspecting the
monitor and its job definition confirms that this is its required source API,
not an optional import. The job was enabled and its recorded last status was
`error`; that status alone does not prove every recorded failure had this cause.

The historical implementation is retained at commit
`ffd4fcb841f60998decc392ba70065f47a9c3749`, together with continuation and cron
integration tests. It introduced bounded standing coordination while preserving
action-grant boundaries. It is an ancestor of the checked baseline, but the
subsequent source rebuild removed the module and its continuation integration
tests from the prior-main tree. The first-parent diff of `2aecdc1` identifies
that removal; its commit message says it promotes the tested rebuild tree while
preserving prior main as a merge parent. Investigate that existing
implementation, the rebuild, and the current notifier interfaces
before choosing a compatible repair. Do not blindly restore the old module or
discard its authority, scope, cancellation, or quiet-wait checks.

The source reproduction is:

```python
import importlib.util

assert importlib.util.find_spec("hermes_cli.kanban_continuation") is None
```

This is a baseline diagnostic, not a proposed regression test asserting that a
module must remain absent. #29 needs an observable owner handoff through the
supported lifecycle, including a missed wake and actual successor acceptance.

### Interactive restart recovery

[`build_resume_recovery_note`](../../gateway/run.py) selects the interactive
branch for an empty recovery message. That branch tells the model to report
restoration, ask for instructions, and skip unfinished work. The default adapter
has `interactive_resume=True`; the production turn runner obtains that flag
from the delivery adapter. A new real user message also receives the blanket
skip guidance, although giving that new message priority is appropriate.

History explains the original safeguard: commit
`75ed07ace82a4bc05458ff827f4ce3750af7a323` prevented a resumed unanswered tool call
from repeatedly restarting the gateway. The current noninteractive branch was
subsequently specialized to finish unattended work. Existing tests verify that
branch and resume scheduling, not the requested interactive task/effect contract.

Reproduce the divergence by using the existing gateway recovery path with a fresh
resume-pending session, no new user message, and an interactive adapter. Compare
with the same interrupted task on a noninteractive adapter. #31 must prove that
the interactive owner continues the first unfinished authorized step after
checking actual effects, without replaying the restart. A newer cancellation or
redirect must win. Do not remove replay cleanup or restart-loop protection.

### Worker and review launch

[`_dispatch_lane_task`](../../hermes_cli/kanban_db_dispatch.py) checks profile
existence, capacity, and respawn guards, claims the task, resolves its workspace,
then adds `sdlc-review` for the review lane and spawns. It does not establish all
required skills or sandbox execution before that spawn.

[`HermesCLI.finalize_preloaded_skills`](../../cli.py) raises when every requested
skill is missing; if some load, it warns and skips missing ones. Thus both an
incompatible review startup and omission of a required review skill need to be
distinguished from an ordinary transient worker failure. The existing CLI tests
verify the entirely-missing startup error, not an owned capability blocker
through dispatch and review.

The checked profile catalog resolves `sdlc-review` for Ang. Reuse that capable
owner; absence in another profile is not itself a configuration defect. #32
should select an incompatible disposable profile to demonstrate the failure,
retain the candidate, then use the supported capable profile or repair the exact
prerequisite and observe successful review. Do not change providers, credentials,
sandbox boundaries, or spending policy to manufacture success.

## Existing work to reuse

The read-only Kanban inventory found the following existing owner work. IDs are
navigation pointers, not authority to reopen or dispatch a card. Refresh status,
scope, later instructions, and current evidence before acting.

| Existing card | Observed owner and state | Relationship to the approved slices |
| --- | --- | --- |
| `operations/t_a23e63ed` | Rook; blocked | Continuation host integration and retained acceptance evidence. Reuse for the installed portion of #29 and #35 where scope matches. |
| `operations/t_99e039c7` | Ops; triage | Installed acceptance and owner relay. Reconcile with Rook's integration card rather than create another relay task. |
| `engineering/t_e4c66a42` | Ang; blocked | Earlier reliability corrections. Consult its preserved work for reusable evidence; do not reactivate the broad plan automatically. |
| `engineering/t_42ca681c` | Ang; triage | Historical qualified-candidate activation. Reconcile the candidate with current source before any release; its state does not justify redeployment. |
| `engineering/t_96797c3c` | Ang; done | Standing continuation admission repair. Its historical source commit is retained in baseline ancestry, but the module is absent from the current tree. Reuse the relevant implementation and tests only after current compatibility review. |
| `engineering/t_9721b49b` | Ang; done | External-executor failure and overdue-wait reporting. Preserve completed status and inspect evidence before assuming coverage is missing or current. |
| `engineering/t_5fa94ccc`, `operations/t_2159f1a6` | Ang and Rook; done | Earlier continuation work. Completed labels do not qualify the current installed path, and the retrospective does not reopen them. |

No existing card was modified, unblocked, dispatched, or recreated for this
inventory. Unrelated bot, voice, memory, backup, and product work stays outside
this acceptance scenario.

## Local evidence navigation

These owner-managed artifacts live outside the source checkout. Paths below are
relative to the installation's shared data root, resolved with
`get_default_hermes_root()`. Read the named artifacts under their owning profile;
do not run the monitor or release scripts merely to inspect this baseline.

| Evidence | Owner-relative locator |
| --- | --- |
| Continuation monitor | `profiles/rook/scripts/mvp-continuation-monitor.py` |
| Continuation job definition and recorded last result | `profiles/rook/cron/jobs.json`; select the job whose script is `mvp-continuation-monitor.py`, without dumping prompts or secrets |
| Continuation guidance | `profiles/rook/skills/hermes/orchestrator-continuation/SKILL.md` |
| Current activation and binding references | `profiles/rook/state/activation.json` and `profiles/rook/state/mvp-continuation/standing-binding.json`; inspect current scope and stops, never treat a historical record as new consent |
| Release procedure | `profiles/ang/skills/repository/release-execution/SKILL.md` (version `2.0.2`) and its `references/gateway-release.md` |
| Tested release machinery | The same release skill's `scripts/` directory, including `tests/conftest.py`, `tests/test_drain_and_release.py`, and `tests/test_recover.py` |

The seven script/test files in the tested kit copy match the owner's originals
byte for byte. Their aggregate SHA-256 is
`c23c96db66757f8b898041bdfcdeb531e816aa0fd22655ea6d4608f07319e1d3`.
For reproducibility, hash the sorted relative names `drain.py`,
`launch-executor.sh`, `new-release.py`, `recover_template.py`,
`tests/conftest.py`, `tests/test_drain_and_release.py`, and `tests/test_recover.py`;
for each, feed its UTF-8 name, a NUL, its file bytes, and another NUL into one
SHA-256 digest. This identifies the tested local kit, not a universal evidence
requirement for other tasks.

## Verification and limits

The prepared Python 3.14.7 test environment was selected explicitly through
`HERMES_PYTHON`; the legacy checkout `.venv` points to Python 3.11 and is not the
test interpreter. Every test invocation uses `scripts/run_tests.sh`, which
clears credentials and supplies isolated per-file state. The runner's disk-backed
temporary directory requires execution outside the filesystem sandbox; its
initial sandbox attempt ran no tests and is not counted as a test failure.

The first focused run passed **74 tests in eight files**, with no failures:

- `tests/gateway/test_kanban_wake_acceptance.py`
- `tests/gateway/test_kanban_notifier_claim_recovery.py`
- `tests/gateway/test_restart_resume_pending.py`
- `tests/gateway/test_goal_resume_restart.py`
- `tests/hermes_cli/test_kanban_handoff_receipts.py`
- `tests/hermes_cli/test_kanban_blocked_sticky.py`
- `tests/hermes_cli/test_kanban_per_profile_cap.py`
- `tests/hermes_cli/test_kanban_credential_startup_exit.py`

The extended run passed **59 tests across six files**, including 22 tests in the
copied release kit. The separate real-process dispatcher-restart file exceeded
its 240-second limit after printing one passing test; its process tree was killed
by the runner. That file did not finish, so the run is not green despite the
runner summary's aggregate “0 failed” test count. Do not count that as successful
dispatcher-restart qualification or immediately relaunch the timed-out process
tree. The passing files were:

- `tests/hermes_cli/test_cli_preloaded_skills.py`
- `tests/hermes_cli/test_kanban_control_holds.py`
- `tests/hermes_cli/test_kanban_review_lifecycle_complete.py`
- `tests/hermes_cli/test_kanban_runtime_survivor.py`
- The existing release kit's `test_drain_and_release.py` and `test_recover.py`

The full project suite was invoked once with `-j 8`. It discovered 5,599 files,
then was stopped after repeated real-home I/O guard failures in existing ACP
fixtures. Those guards refused access to live user state; they were not bypassed.
Other existing assertion failures also appeared. At interruption, the last
progress line reported 368 passed and 40 failed tests, with less than 1% of the
estimated suite complete. These are partial counters, not a full-suite result.
Examples include `tests/acp_adapter/test_empty_session_persistence.py`,
`test_acp_commands.py`, and `test_finish_turn_finally.py`. Runtime source was
unchanged from the baseline during all runs. Repairing unrelated suite fixtures
is not a prerequisite for recording this baseline, but full-suite qualification
has not passed.

The 14 completed scoped files passed 133 tests in total. The real-process
dispatcher timeout and interrupted full suite remain explicit verification
limits. No new tests or runtime behavior are introduced by this baseline record;
typechecking is not applicable to the documentation-only change.

`python3 scripts/check` completed all **12 blocking checks successfully**.
`git diff --cached --check` passed, and all three local Markdown links resolve.

The installed gateway status file records the same code SHA as the checked
baseline. A read-only process check using the existing gateway-command classifier
and a service query using the existing user-bus environment resolver also
confirmed the matching live service process. This establishes installed runtime
identity and liveness, not route readiness, successful downstream execution, or
activated configuration. The initial unbound service query could not reach the
user bus; binding the existing resolver was sufficient. No restart, deployment,
inference probe, or live failure injection was performed.

Release-kit tests are run from a disposable copy. Their fixtures replace
`systemctl` and the Hermes CLI with shims. Their results can establish recovery
decisions, qualified-source rejection, and ownership of pause restoration;
native process lifetime must still be established by #33 on the host where the
supervision operates.

## Bounded acceptance scenario

Use the existing two boundaries, without a new service, dispatcher, ledger,
model-facing tool, or exhaustive topology matrix:

1. In disposable homes, profiles, boards, and a repository, submit one approved
   bounded feature through Sunny to Rook and Ang. End the initiating turn. Retain
   current scope, approval reference, owner, phase, next action, and evidence.
2. Complete implementation and review through supported lifecycle transitions.
   Refuse or omit one wake. Let existing reconciliation recover it and observe
   the receiving owner's actual work acceptance or a concrete delivered blocker.
   Repeat unchanged reconciliation and observe quiet behavior and no duplicate
   downstream effect.
3. Park a specialist on a real prerequisite with a manual hold present. Observe
   unrelated eligible work using the released capacity without clearing the
   prerequisite or hold. Resolve only the real prerequisite and verify the
   original task remains resumable under current authority.
4. Interrupt after one recorded mutation but before closeout. Resume through the
   gateway, inspect the real effect, and perform only the next unfinished step.
   Exercise a newer cancellation or redirect at the same boundary.
5. Demonstrate a missing required review skill or unusable workspace route with
   disposable profiles. Observe one useful owned blocker, preserved candidate
   work, and no identical-launch loop. Correct the prerequisite or select the
   capable owner, then observe actual successful review or execution.
6. Declare one required artifact and one optional unrelated discovery. Clean the
   scratch workspace through the supported lifecycle. A fresh owner reads the
   durable artifact and compact current work order; review judges the original
   outcome. Missing required evidence prevents acceptance; the optional discovery
   does not become a gate. Judge scope in a bounded agent exercise, not a keyword
   or prompt-snapshot test.
7. In native disposable services, end the release executor after stop and before
   start. Observe independent recovery of validated source, preserved unrelated
   holds, and an actual result. Unsafe source remains stopped with an actionable
   recovery-required result through a route independent of the stopped gateway.
8. After the affected installed paths are ready, #35 runs one currently approved
   bounded feature through the real owner chain. Ops participates only where
   genuinely required. Exercise a restart or park, verify actual changed runtime
   behavior and final reporting through the authorized route, and require no
   additional message from Brian. Keep failure injection disposable.

No prefactor is demonstrated as necessary for this baseline. #29 should first
resolve the existing adapter/source mismatch; #31 should use the existing
recovery seam; #32 should use existing profile and skill resolution; #33 should
qualify the existing kit; #34 should use existing task records and attachments.
If any repair needs a new persistence contract or unavoidable prefactor, prove
that gap and order the smallest compatible change before the affected repair.
The overall #27 delivery outcome remains incomplete until installed acceptance
passes or an exact human-only blocker is surfaced.
