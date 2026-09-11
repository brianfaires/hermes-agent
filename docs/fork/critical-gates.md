# Approved Critical restoration register — v0.21 restoration

Brian approved all documented restoration slices for source/tests/staging on
2026-09-10 (root Kanban comment 16:51, recorded in the approved restoration
brief). **The historical decision requests and “pending approval” language below
are superseded. Do not request the same approval again.** Critical classifications
and the described compatibility/security contracts remain. Human UAT is deferred
and never gates development. Retired consumers, archive-only new features and
Discord voice remain excluded; no live mutation, activation, push or main change.

Implementation status and exact evidence live in V021_RESTORATION_LEDGER.json.
The following anchors and original decision text are preserved as historical
design provenance, not current approval holds. OLD is
`2945588a014543d47c9e5e4a0d92ba6e361387c1`; BASE is
`cf62291dad2cd9c12de80bc5194ff76ef0f6ce55`.

## G17 — profile identity, credentials, routing and durable ownership (FC17; related FC10)

OLD gateway/authz_mixin.py:327-346,525-542 chooses transport pairing identity; BASE:473-486,695-711 chooses source profile/default. OLD gateway/session.py:1470-1493,2591-2755 reconciles durable owner; BASE:1990-2008,3552-3620 has different recovery/switch semantics. OLD gateway/config.py:1464-1467 and Discord adapter:132-149,6801-6814 bridge top-level policy; BASE config:1702-1784 and adapter:6680-6711 have different precedence. OLD secret_scope.py:92-116 and scheduler:2785-2815 use private refresh; BASE secret_scope:289-310, scheduler:5909-5925,7205-7248 use different scope/environment contracts. OLD webhook.py:615-624,698,841-843,911-914 namespaces adapter identity; BASE:730,857,926-951 does not.

Decision requested: approve a separately tested restoration of transport-bound pairing, durable session owner verification, profile-local credential refresh, policy bridge precedence, and webhook profile namespace, retaining current stronger authorization checks? These alter principals, persisted ownership and/or routing; a nominal tightening is still a trust-boundary change. Recommendation: approve only after a concrete per-path design and old/current executable identity matrix; do not approve blanket FC17 parity. No live identities/configs examined. Noncredential inventory and worker scope remain separable.

## G19 — notification destination policy (FC19)

OLD hermes_cli/kanban_notifications.py:39-160 defines origin/deny/telegram_home_only, malformed-policy denial, allowed-platform exceptions and TUI preservation. BASE gateway/kanban_watchers.py:474-478,519-558 delivers subscription destinations directly; CLI kanban.py:3040-3054, tools/kanban_tools.py:1592-1601 and slash_commands.py:650-663 no longer resolve that policy.

Decision requested: approve reinstating policy resolution consistently at subscription and delivery, including rerouting to Telegram home and treatment of existing subscriptions? Impact: changes recipients and suppresses some notifications; potential privacy/identity consequences. Recommendation: explicit policy approval and isolated destination matrix before code changes; retain subscriptions as data and avoid destructive migration. Diagnostics are separable.

## G33 — Tirith verdict/filter policy (FC33)

OLD tools/tirith_security.py:830-872,891-916 suppresses exact package-name matches and evaluates all findings before display truncation. BASE:830-855 caps findings first, then suppresses .app-only warns. The historical change can convert warn to allow; moving the cap also changes the effective verdict for mixed findings beyond the cap.

Decision requested: approve exact-name warning suppression and/or separately approve full-findings evaluation before the display cap? Recommendation: full-findings evaluation is a useful safety correction, but both affect the scanner authorization verdict and remain gated under the explicit trust-boundary rule. No mutation of Tirith here.

## G26/G27 — prompt cost and execution policy

FC26 OLD agent/skill_utils.py:782-800 defaults descriptions to 1024 chars; BASE:1182-1204 uses 60. Decision requested: approve raising the default prompt footprint to 1024? Recommendation: preserve 60 pending approval; explicit caller cap and future-build invalidation are separable.

FC27 OLD agent/prompt_builder.py:398-410,467-485 and system_prompt.py:284-293 select bounded gpt-5.6-sol/terra execution guidance; BASE prompt_builder:529-539 and system_prompt:588-617 use generic persistence. Decision requested: approve restoring model-specific one-verification/proportional-stop policy? It changes execution/cost policy. Recommendation: restore only those configured model variants after explicit approval, with frozen-session prompt tests.

## G30 — CI publication and process-killer boundary

OLD .github/workflows/osv-scanner.yml:40-103 separated scanning from fork SARIF publication; BASE:40-74 delegates publication to pinned reusable workflow. OLD tests/conftest.py:724-812 has wrapper-aware process-killer parsing; BASE:1425-1431,1466-1497 token scanning differs. Decision requested: separately approve SARIF publication-policy restoration and any change to the allowed/blocked process-command classification? Recommendation: do not weaken CI or test safety under a generic restoration; qualify a command matrix first. No CI/security guard changes made.

## G17R — profile-qualified active cron registry and shutdown compatibility (FC17)

Decision requested: approve a coordinated restoration of profile-qualified active job keys across scheduler registration/release/stale-sweep/shutdown and manual-run consumers, with the existing global drain and ownership fences preserved. This slice is gated for uncertain compatibility and interruption-boundary classification; a scheduler-only key replacement is unsafe.

Old anchors: `2945588a:cron/scheduler.py:379-400` uses `(resolved_profile_home, job_id)` and captured contexts. Current baseline anchors: `cf62291dad:cron/scheduler.py:849-903` exposes raw IDs through shared registration/getter; `:1093-1187` compares them against the active profile's durable execution ledger; `:1288-1310` shutdown mixes registered IDs with execution-owner records; `:8172-8176` releases after leaving the copied worker context. `tools/cronjob_tools.py:1281-1283` treats the same getter as a per-job manual-run precheck, while `gateway/run.py:9253-9254,9483-9485` uses it for global shutdown drain counts.

Executable current evidence: `../fc17-registry-probe.py` imports real scheduler and installs real home ContextVars for two temporary homes. Same ID registration returns `[true,false]`, despite distinct resolved homes; see `../fc17-registry-result.json`. No persisted jobs or model execution involved.

Proposed shape: explicit captured profile-qualified keys internally; pass the key/context through executor release; filter stale reconciliation to the owning store; preserve global drain visibility using a separate global snapshot while manual-run checks query current profile; maintain execution-token and expected_fire_owner fencing, with equal-ID parallel-profile and shutdown/stale-recovery tests. No pools, credentials, destinations, authorization or .env behavior changes. Impact: prevents one profile's equal-ID job blocking another; careless implementation can release/interrupt the wrong profile or change public getter semantics. Recommendation: approve only this coordinated, tested contract after Brian reviews compatibility implications. No registry source changed in this implementation pass.

## G25 — rollback after gateway follow-up persistence failure (FC25)

OLD gateway/slash_commands.py:3738-3793 invokes `discard_failed_compression_child(..., discard_messages=True)`, restores goals/logging/current identity, and reopens the parent. BASE agent/conversation_compression.py:4758-4807 atomically publishes a populated child with lease/watermark and concurrent-tail handling. BASE hermes_state.py:7369 `reopen_orphaned_compression_session` explicitly refuses reopening a parent with any canonical continuation; the old discard helper is absent. BASE slash_commands.py:4913-4922 still raises on follow-up rewrite failure.

Decision requested: approve a redesigned transactional rollback/reconciliation that may discard a published continuation and move ownership back, with explicit concurrent-child-message/lease rules? Blindly restoring old deletion would conflict with stronger current lineage safety and can lose concurrent data. Recommendation: prefer eliminating redundant gateway rewrites where possible and atomically committing partial-compression tail before publication; qualify that design before any deletion/identity changes. No rollback deletion implemented. The independent no-rotation failure-bookkeeping guard is restored and tested.

## G28 — pip-compatible Discord crypto packaging (FC28)

OLD pyproject.toml:164 and tools/lazy_deps.py:161-172 replaced `discord.py[voice]` with base discord.py plus explicit PyNaCl 1.6.2/davey. BASE pyproject.toml:199-203,397-409 and lazy_deps.py:212-219 use voice extra with a uv-only PyNaCl override. Restoring explicit dependencies changes installer resolution/voice packaging; adding the higher PyNaCl floor without removing the extra conflicts with its declared cap. Decision requested: approve the explicit-package replacement after a pip and uv resolution matrix (no installation). Recommendation: preserve present voice-extra interface pending that compatibility qualification, independently of restored core ASGI floors. This is not a claim of deployed vulnerability or parity.
# FC18 / FC20 approval decisions

These are precise affected slices, not a gate on the safe inventory and lifecycle restorations already implemented.

## FC18 persistent `dir` branch creation / dashboard branch input

Old main `2945588a:hermes_cli/kanban.py:1434-1436` accepted `--branch` for persistent `dir` as well as `worktree`; `hermes_cli/kanban_db.py:2545-2602` normalized metadata. Current `hermes_cli/kanban.py:_cmd_create` rejects branch unless worktree; `hermes_cli/kanban_db.py:create_task` repeats that restriction. Current `plugins/kanban/dashboard/plugin_api.py` create model/forwarding lacks `branch_name`.

Proposed decision: permit persistent-directory branch metadata and expose the field through dashboard create/update, retaining all present filesystem and delegated mutation restrictions. Impact: expands presently accepted workspace metadata and dashboard write interface. Classification uncertain because persistent workspace authority and compatibility are implicated. Recommendation: approve only after Brian confirms intended persistent-dir semantics; no mutation made here.

## FC18 normal write-boundary metadata validation

Old `2945588a:hermes_cli/kanban_db.py:2545-2602;2794-2799;6846-6882` invoked shared normalizers at create/set. Current `create_task` validates kind and worktree-only branch but does not validate absolute paths/ref syntax; current `set_workspace_path` and `set_branch_name` execute direct SQL. Current `normalize_workspace_metadata` / `normalize_branch_name` remain used by decomposition (`decompose_task`, around line 7602).

Proposed decision: call the existing normalizers on normal creates and setters, retaining stronger current worktree-only creation restriction until separately approved, and define how callers with legacy relative paths/placeholders are handled. Impact: callers currently storing relative paths or invalid ref strings would begin receiving ValueError; normalizer trims strings and expands `~`. This is an input-compatibility change, not the specifically preauthorized scanner/cleanup-refusal exception. Recommendation: Brian approve focused validation restoration after compatibility decision. No mutation made here.

## FC20 broader environment isolation design

Old `2945588a:tools/environments/local.py:452-471` scrubbed every HERMES_KANBAN_* and assigned scratch roots; old `hermes_cli/kanban_db.py:376-408;546-558` redirected delegated mutations to disposable boards. Current `agent/delegation_context.py:scrub_kanban_env` removes a listed set and stamps lineage; current `hermes_cli/kanban_db.py:_assert_not_delegated_child_mutation` refuses writes at durable boundaries.

Recommendation: preserve current refusal-based isolation (stronger than allowing scratch-board writes); do not restore disposable-board behavior. Extending scrubbed keys such as HERMES_KANBAN_HOME/ATTACHMENTS_ROOT changes subprocess path exposure/identity boundaries and remains unimplemented pending explicit approval and consumer-by-consumer review. This is not asserted as full historical parity. Existing context predicate is reused only to suppress worker guidance/lifecycle attempts from children/cron, without weakening durable restrictions.

## G32 — standalone test thread policy (FC32)

OLD scripts/run_tests_parallel.py:331-342 forced six BLAS/OMP variables to 1. BASE `_run_one_file_once` copies caller environment, and scripts/run_tests.sh does not set these limits. Decision requested: restore unconditional one-thread ceilings, or adopt default-only behavior? Both affect caller resource/performance policy, and the latter is not exact historical parity. Recommendation: explicit policy decision; failed-spawn cleanup is already restored separately.

## G11 — ancillary strict cron name/toolset validation (FC11)

OLD cron/jobs.py:422-450 validates plain string names and list-of-string toolsets; BASE create_job:2399 stringifies toolsets and update_job:2639-2720 accepts broader values. Decision requested: approve reinstating strict normalizers outside the intentionally replaced Calendar integration, including rejection behavior for existing non-string callers? Recommendation: restore with an explicit compatibility contract and tests. This is distinct from the specifically authorized strict prompt scanner coverage, already restored.

## G14/G15 — Discord text compatibility and threading

OLD Discord adapter:100,225-244,5831-5841 escapes *_~|> throughout prose; BASE adapter:5806-5814 and tests/gateway/test_discord_format.py preserve rich emphasis/table labels. Decision: restore blanket literal rendering or retain current rich-markdown contract? Recommendation: preserve current until a literal-context interface is approved. Compact previews and progress embed suppression restored independently.

OLD gateway/platforms/base.py:1534-1558,3882-3908 recognizes standalone MEDIA lines; BASE:2073-2115,5184-5228 explicitly accepts inline/glued directives, spaced paths and CJK punctuation. Decision: authorize standalone-only attachment interpretation, preserving current path restrictions/code masks? This changes an outbound attachment interface with privacy implications; no parser mutation made.

OLD Discord adapter:8672-8685 retains configured auto-threading for free-response channels; BASE:8284-8293 excludes free channels. Decision: approve historical auto-thread routing for configured free-response channels while retaining current allow/deny/reply/no-thread/voice-linked exemptions? Recommendation: approve as a separate routing-policy slice after confirming desired channel behavior. Slash-sync retry state is independently runtime-verified.
