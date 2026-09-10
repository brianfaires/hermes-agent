# Bounded continuation qualification

Base `3c8f7d0d160883dc48a169d12f7e3cdca203b58d`; new tests owned by
`76d908a34804889c9c27cebd08619e71528070e4`. No production code changed.
Human UAT is deferred and does not block separable development. Existing
Critical decisions remain at their original classification.

## Qualification-only entries

| Entry | Contract and evidence | Limits |
| --- | --- | --- |
| FC-02-E03 | `tests/hermes_cli/test_v021_install_launcher_guard.py`: executes the complete extracted `setup_path` shell function with a `.git` file and verifies an unchanged temporary tree. | No bootstrap/install execution; Linux shell guard only. Explicitly permitted source extraction, not a source-pattern assertion. |
| FC-17-E01 | Existing `test_model_command_profile_config.py::test_model_picker_reads_routed_profile_config` reads the secondary config; `test_model_picker_persist.py::test_multiplex_picker_global_persists_only_named_profile` invokes the captured callback **after leaving** the original profile scope and proves only the named YAML changes. | Provider resolution/transport are fakes; no claim about all command routing or credential refresh. Exact-file hosted receipts below. |
| FC-17-E02 | `test_v021_profile_scope_qualification.py`: real `_profile_runtime_scope` reads two temporary `.env` fixtures, propagates to `asyncio.to_thread`, restores nested scopes on body exception and leaves process environment unchanged. | Does not qualify an exception during scope setup or external secret-source refresh; G17 remains. |
| FC-17-E07 | Existing `test_cron_status_profile_isolation.py::TestGetServicePidsProfileScope`: default exact systemd unit vs fleet glob returns the respective PIDs. | systemctl output is mocked at the subprocess boundary; no host service operation. |
| FC-17-E10 | `test_v021_cron_startup_qualification.py`: real `start_gateway` resolves allowlisted homes and real built-in provider; invokes the captured provider with exact startup kwargs, observes each scoped store/owned adapter, and checks real heartbeat files. | Startup side effects and thread launch are stubbed. Tick execution is a recording boundary; no scheduled jobs/models/delivery run. Existing provider tests supplement absent/empty adapter cases. |
| FC-17-E11 | `test_v021_profile_scope_qualification.py`: heartbeat resolves active profile plus explicit `use_cron_store` precedence; success stays absent for liveness-only tick and outer bytes are preserved. | Temporary files only. |
| FC-19-E06 | `test_v021_notifier_profile_qualification.py`: real disposable SQLite task/subscription/event and watcher deliver a secondary-only platform through that secondary adapter. | Adapter records locally. This proves map inclusion/selection, not notification policy G19. |
| FC-32-E03 | Current `test_run_progress_topics.py::_make_runner` lacks historical `_gateway_profile_home`; current `gateway/run.py` has no consumer of that attribute. All 28 current file tests pass hosted. | Retires the obsolete fixture assignment, not a production profile-scoping contract. Old assignment at `2945588a:tests/gateway/test_run_progress_topics.py:280-302` is historical evidence only. |
| FC-36-E01 | `test_v021_restart_preflight_qualification.py`: actual handler reports all_profiles, two agents plus cron/API work, writes dry-run audit and makes no reservation/request. Existing restart plugin and delivery tests cover accepted scheduling with active work. | No service detection or real restart; counts from subsystem boundaries are fixture values. Authorization/audit/cooldown remain unchanged. |
| FC-36-E10 | `test_v021_restart_teardown_qualification.py`: real request → drain → stop cancels a decoy task, preserves restart orchestration, visits primary/secondary adapters, clears maps, sets shutdown event and service exit code. Existing five delivery regressions and 14 drain tests supplement pre-stop wait. | Host cleanup/process/watchdog operations and transport are stubbed. Graceful empty-drain traversal is qualified; no blanket timeout/OS teardown equivalence. |

Seven new tests in six files passed local unittest under the canonical
interpreter read-only, bytecode disabled, source PYTHONPATH, clean environment
and fresh HOME/HERMES_HOME/HERMES_BUNDLES_DIR. No pytest installation attempted.
New hosted collection is Ang's next integration check.

## Reused hosted evidence

`V021_FINAL_CI_3c8f7d0.json` preserves the accepted receipt for exact SHA
`3c8f7d0d160883dc48a169d12f7e3cdca203b58d`, run **34523897060**, aggregate
SUCCESS (28 successful jobs, six skipped), including all **52 added tests**.
`V021_CONTINUATION_RECEIPTS.json` adds exact matching file-log lines for the
qualification above. These tests/source files are unchanged by this test-only
continuation, except that ledger candidate blob metadata is refreshed to the
actual base. The older e36f3d7 failure and correction review remain historical.

## Gated compatibility design — no affected implementation

The following matrix makes specific remaining decisions reviewable. A source
prediction is explicitly different from an executed old runtime. Failed
proposed contracts stay failed; neither green CI nor nearby qualification
changes any Critical gate.

| Gate / fixture | Old result | Current result | Minimal proposed contract / decision |
| --- | --- | --- | --- |
| G11: `name=123`; `enabled_toolsets=[123]` | Source: `_normalize_explicit_name` and `_normalize_toolset_list` reject, `2945588a:cron/jobs.py:422-450`. | Executed real `create_job`: stores numeric name; stringifies toolset to `"123"`. | Decide strict rejection on create/update, preserving legacy reader tolerance; no automatic rewrite of stored jobs. |
| FC18: relative dir path; `bad..ref` worktree branch | Source: normalizers reject, `2945588a:hermes_cli/kanban_db.py:2545-2602`. | Executed real `create_task`: both persist. | Decide validation at normal create/set boundaries and compatibility for existing callers; preserve delegated durable-write refusals. |
| FC18: persistent dir with `feature/test` branch | Source: old normalizer permits persistent dir. | Executed create rejects `branch_name is only valid for worktree workspaces`. | Separate interface decision from validation: approve dir metadata semantics and dashboard field propagation before expanding accepted input. |
| G19: origin / deny / malformed policy `17` | Executed full historical helper: origin / no target / no target. | Executed real current watcher with temp board + recording adapter: origin for all three. Proposed deny/malformed contracts **fail**. | Resolve policy at subscription and delivery consistently; retain subscription data; decide home rerouting and TUI/allowlist exceptions individually. |
| G17R: equal job ID in two homes | Source: old `(resolved home, id)` registration permits both; old runtime was not rerun. | Accepted prior real scheduler receipt `../fc17-registry-result.json`: `[true,false]`. | Capture qualified keys through register/release/stale sweep; maintain global drain snapshot and profile-local manual precheck separately. No getter-only or scheduler-only substitution. |
| G33: 50 suppressible `.app` findings followed by another warning, scanner exit 2 | Executed full old module: warn, 50 displayed findings. | Executed current module: allow, zero findings. Proposed full-findings contract **fails**. | Decide full-findings verdict evaluation independently of exact-package-name suppression. Keep external scanner exit semantics and bounded display; do not change authorization without approval. |

Detailed commands, raw output and probe paths are in `../continuation-evidence.md`.
G11/FC18 fixtures only write disposable stores; G19 invokes the old helper from
a read-only old blob materialized in the task evidence folder, never its old
watcher. G33 replaces external binary resolution/execution with the same fixed
payload for both complete modules; no binary installation or command execution.

Other gated designs retain the concrete anchors in `critical-gates.md`:

- G17 pairing/owner recovery/config precedence/credential refresh/webhook identity:
  distinguish transport principal, conversation profile and durable owner in each
  API. Require disagreement, missing-profile and restart recovery cases before
  approval; the new scope test does not decide which principal owns a request.
- G25: preserve canonical child, lease and concurrent tail on post-publication
  failure. Prefer committing the necessary tail before publication; a legacy
  delete/reopen helper cannot satisfy the current orphan refusal. No deletion.
- G14/G15: retain rich markdown, current MEDIA parsing and current free-response
  routing until their separate compatibility/privacy choices are made. Test
  standalone versus inline/code-masked MEDIA and explicit thread exemptions
  against the chosen contract; no blanket literal/auto-thread rollback.
- G20: retain durable child-write refusal; decide extra environment scrubbing
  only after identifying consumers of each path/identity variable.
- G26/G27/G32: distinguish configurable description caps from default prompt
  cost, model-specific stopping policy and caller thread ceilings. Preserve
  cached conversation bytes and explicit caller settings until each decision.
- G28: current manifest uses `discord.py[voice]==2.7.1` plus a uv PyNaCl override;
  a higher direct floor alone conflicts with the declared extra cap. Qualify a
  proposed explicit dependency replacement with pip and uv resolution after the
  packaging choice; no installer/lock/voice-source change in this continuation.
- G30: fork SARIF publication and process-killer argv classification remain
  separate decisions. A wrapper/flag-value/ancestor matrix must preserve current
  refusals; test-process cleanup receipts do not approve classifier changes.

Ledger reconciliation found no additional ungated production restoration
among the existing 241 subfeatures. All ten qualification-only entries now have
named evidence; 46 Critical subfeatures stay parked, independently of deferred
human UAT. Broader gated design matrices above remain follow-up work where
explicitly identified, not assertions of full historical parity.
