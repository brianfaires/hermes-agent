# Feature disposition ledger — Hermes v0.21.0 reconstruction

INVENTORY MILESTONE (accepted map-only checkpoint). Mechanical coverage and a native second-pass semantic review were completed on recovery worktree `t_99b38c75` / task `t_5d084747` without Codex (local Codex task quota exhausted until 2026-09-07 17:45; CLI also stale on models-catalog `max` effort). This ledger authorizes disposition accounting and bounded non-Critical migration planning only. It does **not** authorize Critical/product implementation, live config changes, deploy/restart, or retirement of any still-running behavior beyond what current v0.21 already does.

This is the authoritative feature-cluster map for the preserved pre-cleanup fork. Absence from the reconstruction branch is never an implicit drop.

## Scope and rules

- Reconstruction baseline inspected: `495b851fbc0b8b3531e56db5373b4bf4446ff288`.
- Protected source namespace: `refs/backup/hermes-v0.21.0-precleanup-20260903/*` (30 refs, 22 unique tips).
- Complete legacy-only union: **157 commits**, including **2 merges**, reachable from the protected refs and absent from the baseline.
- `I001`–`I157` below are the immutable commit inventory. Each inventory item appears exactly once in the appendix and points to one or more behavior clusters. Multiple cluster IDs on an item are an intentional mixed-commit split.
- Each feature cluster appears exactly once in the disposition matrix. The omission and Brian-UAT ledgers are indexes into these IDs, not competing disposition authorities.
- Allowed decisions are `KEEP`, `REWRITE`, `DROP_UPSTREAM`, `DROP_LOW_VALUE`, `DROP_OUT_OF_SCOPE`, `DEFER_HUMAN_VALUE`, and `DEFER_REPRODUCTION`.
- Placement order is existing behavior/config → CLI+skill → service-gated tool → standalone/profile plugin → platform adapter → small generic seam → core only as a last resort.
- Historical dependency pins are regenerated from the v0.21 graph; they are never replayed because an old commit exists.

## Pre-mutation baseline

Sole-writer preflight found the reconstruction worktree clean on `brian/reconstruct-v0.21.0`; local HEAD and `origin/brian/reconstruct-v0.21.0` both resolved to `495b851fbc0b8b3531e56db5373b4bf4446ff288`. The canonical checkout was not used for edits: its pre-existing local `main` tip was `2945588a014543d47c9e5e4a0d92ba6e361387c1`, local `staging` was `07677f36f5f005f2521dadc926b7e7d572acd760`, and its pre-existing untracked `settings/local.json.default` was left untouched.

## Disposition counts

| Decision | Clusters |
|---|---:|
| `KEEP` | 8 |
| `REWRITE` | 2 |
| `DROP_UPSTREAM` | 24 |
| `DROP_LOW_VALUE` | 5 |
| `DROP_OUT_OF_SCOPE` | 3 |
| `DEFER_HUMAN_VALUE` | 5 |
| `DEFER_REPRODUCTION` | 4 |
| **Total** | **51** |

## Complete behavior-cluster disposition

| ID | Behavior | Source inventory | Decision | v0.21/current value evidence | Placement / gate |
|---|---|---|---|---|---|
| FC-01 | Concurrency-safe structural `hermes config patch` mutations | I001 | `DEFER_REPRODUCTION` | v0.21 has fail-closed atomic `config set/unset` and literal-dot handling, but no structural patch command or retained behavior test; live configs contain no evidence that the JSON-Pointer batch interface is still used. | Reproduce against the existing config CLI; extend that CLI only if an atomic multi-mutation need remains. |
| FC-02 | Installer refuses to replace the canonical launcher from a Git worktree | I002 | `KEEP` | Live gap confirmed: doctor `--fix` and setup/install path setup could repoint `~/.local/bin/hermes` from a linked worktree (`.git` file). Guarded doctor repair, `setup-hermes.sh`, and `scripts/install.sh` `setup_path`; focused doctor + setup script regressions pass. | Installer/doctor only; no core tool surface. |
| FC-03 | Browser connector tolerates an unavailable IPv4/IPv6 loopback family | I003 | `KEEP` | Live RED on this intentionally IPv4-only host: pre-fix `find_free_debug_port` required both families, so every candidate failed the missing IPv6 bind and returned occupied `preferred+1`. Fixed to probe available loopback families first; dual-stack discovery tests plus new unavailable-family regression remain green. | Existing `hermes_cli/browser_connect.py` only; checkpoint after this slice. |
| FC-04 | Configurable TTS provider controls and OpenAI-compatible display fallback | I004, I042 | `DROP_UPSTREAM` | v0.21 `tools/tts_tool.py` already has bounded provider settings, xAI/OpenAI-compatible handling, and non-premium fallbacks; live Discord voice remains configured without the legacy patch stack. | Use current TTS configuration and provider adapter behavior. |
| FC-05 | Hindsight availability is independent of the file-memory toolset | I005 | `DROP_UPSTREAM` | v0.21 exposes memory through provider plugins and service-gated memory tools rather than the legacy file-memory bundle, including `plugins/memory/hindsight`. | Current memory-provider plugin/tool gating. |
| FC-06 | Tool-search platform pinning keeps required tools visible | I006 | `DROP_UPSTREAM` | Current tool search keeps core tools visible and honors enabled toolsets before deferring plugin/MCP schemas. Read-only config evidence shows `pinned_toolsets` exists but is empty, so the legacy special-case has no current consumer. | Current `tools/tool_search.py` visibility contract. |
| FC-07 | Langfuse neutralizes multiline path-like text payloads | I007 | `KEEP` | The observability plugin is enabled in read-only live config. v0.21 redacts data URIs and structured content, but it lacks the legacy multiline absolute-path guard and its behavior test, so the SDK can still misclassify tool text as a local file path. | Port only the plugin-local guard and behavior test; privacy/observability review required, no core change. |
| FC-08 | Disk-cleanup recursively enforces wildcard policies | I008 | `KEEP` | Retained and verified in the first reconstruction batch; the current live plugin has tracked wildcard policies. | Bundled disk-cleanup plugin; checkpoint `brian-rebuild-v0.21.0-disk-cleanup-wildcards`. |
| FC-08B | Disk-cleanup protects durable scripts for active and named profiles | I134 | `KEEP` | Retained and verified in the first reconstruction batch; the plugin is enabled in read-only live config evidence. | Bundled disk-cleanup plugin; checkpoint `brian-rebuild-v0.21.0-disk-cleanup-scripts`. |
| FC-09 | Google Pub/Sub OIDC authentication in the generic webhook adapter | I009 | `DROP_LOW_VALUE` | No webhook routes are configured in read-only current config evidence. v0.21 has scoped JWT verification for its current Chronos endpoint, while the legacy change couples Google-specific auth to the generic adapter. | If demand returns, ship a service-gated webhook/platform plugin, not generic core. |
| FC-10 | Guarded local webhook script triggers and process-tree cleanup | I010, I034, I035, I071 | `DROP_LOW_VALUE` | No current webhook route uses script execution. The legacy feature creates an unattended local-code-execution surface plus multiplex state, serialization, and tree-kill obligations without a current consumer. | A future consumer belongs in a narrowly permissioned plugin/service, not the base webhook adapter. |
| FC-11 | Google Calendar cron synchronization and lifecycle reconciliation | I011, I014, I046, I057, I058, I110, I124, I133 | `DEFER_HUMAN_VALUE` | The legacy plugin and its cron hook/event-bus seam are absent; v0.21 instead has `plugins/cron_providers`. Read-only config enables the old plugin id but explicitly sets `cron.calendar_sync.enabled: false`, so current intent is ambiguous. | Brian must choose retirement or a fresh provider/standalone-plugin rewrite; do not restore speculative core hooks. |
| FC-12 | Cron delivery alerts and thread diagnostics | I012 | `DROP_UPSTREAM` | v0.21 has current cron doctor, delivery preflight, profile-aware routing diagnostics, and relay-fronted delivery errors; the legacy warning patch targets an obsolete execution/delivery shape. | Current cron CLI/runtime diagnostics. |
| FC-13 | File-backed cron prompts across tool and API | I013 | `DROP_UPSTREAM` | The current cron job model/tool already accepts `prompt_path` with scheduler/runtime support. | Current cron job/tool schema. |
| FC-14 | Discord markdown, MEDIA-directive, and compact tool-output rendering | I015, I020, I021, I078 | `DROP_UPSTREAM` | Current gateway/Discord delivery has structured media handling, directive validation, platform markdown rendering, and bounded tool activity output. The old source-regex-shaped tests are not the current contract. | Current gateway rendering and Discord adapter. |
| FC-15 | Discord free-response threading and slash-sync retry fingerprints | I016, I017 | `DROP_UPSTREAM` | Current Discord adapter persists responded-thread participation and owns current native command synchronization/retry behavior. | Current Discord platform adapter. |
| FC-16 | Discord outbound sends obey configured channel policy | I019 | `DEFER_REPRODUCTION` | Read-only live config confirms Discord channel policy is configured, but the legacy outbound-fence test is absent and current evidence proves profile-scoped inbound policy, not every outbound path. | Reproduce at the Discord adapter boundary; authorization/external-send change requires explicit Critical approval before implementation. |
| FC-17 | Profile-scoped gateway/Discord multiplex routing and standalone delivery mirrors | I022, I028, I052, I065, I068, I069, I070, I079, I096, I098, I099, I100, I101, I121, I126, I140 | `DROP_UPSTREAM` | v0.21 has a larger session-scoped multiplex architecture, profile secret scopes, per-profile adapters, route provenance, pairing isolation, and profile-aware cron/delivery. Read-only live config has multiplexing enabled. | Current gateway session/profile routing and platform adapters. |
| FC-18 | Kanban board paths plus workspace/branch metadata | I023, I024, I060 | `DROP_UPSTREAM` | Current Kanban DB/tools expose board identity, workspace kinds/paths, deterministic branches, artifacts, parents, and project worktrees with schema validation. | Current Kanban DB and tools. |
| FC-19 | Kanban notification routing policy and fail-closed validation | I026, I033, I080, I113 | `DROP_UPSTREAM` | Current Kanban tooling/watcher paths own notification delivery, run status, and malformed-policy handling; background delivery is opt-in by current tool contract. | Current Kanban watcher/notifier boundary. |
| FC-20 | Kanban worker-only guidance and delegated-child lifecycle isolation | I045, I125 | `DROP_UPSTREAM` | Current `model_tools.py`, `tools/kanban_tools.py`, and `agent/turn_finalizer.py` gate lifecycle tools by dispatcher ownership; `tests/tools/test_delegate_kanban_isolation.py` is present. | Current dispatcher-owned worker lifecycle. |
| FC-21 | Dedicated Human Action Brian Queue | I123 | `DROP_UPSTREAM` | Current Kanban has typed `needs_input` blockers, task comments, triage, subscriptions, and durable run handoffs; a second queue would duplicate the human-action state model. | Use current blocked/triage/comment workflow. |
| FC-22 | Kanban operator holds stay non-ready and live workers are not reclaimed at max runtime | I130, I137 | `KEEP` | The two preserved post-v0.21 side branches contain focused fixes for active Kanban behavior. Current code still owns blocked-state promotion and max-runtime reclaim, while the legacy regression tests are absent. | Migrate as one isolated Kanban state/lifecycle boundary with fresh RED/GREEN proof and explicit approval. |
| FC-23 | Durable Discord Kanban mirror, reply routing, cancellation, and reconciliation | I025, I055, I056, I081, I082, I083, I084, I085, I086, I087, I088, I090, I091, I092, I093, I094, I095, I111, I114, I115, I116, I117, I118, I119, I122 | `DROP_LOW_VALUE` | The legacy subsystem spans schema, daemon, platform plugin, reactions, thread lifecycle, and recovery. Its plugin is absent and read-only config explicitly has `kanban.discord_mirror.enabled: false`. | Do not recreate cross-platform core coupling; a future need should be an external/platform plugin. |
| FC-24 | Discord voice STT aliases, acknowledgements, mixer, stop, and profile routing | I018, I027, I048, I049, I050, I051, I053, I059, I077 | `DROP_UPSTREAM` | Current Discord plugin contains `voice_mixer`, installs it on connect, loads profile-scoped `discord.voice_fx`, and supports acknowledgement/voice orchestration. Read-only live config has auto-voice and voice FX enabled. | Current Discord platform plugin and TTS/STT adapters. |
| FC-25 | Voice-aware and atomic conversation compression/rotation | I029, I030, I031 | `DROP_UPSTREAM` | v0.21 compaction has transactional publication, leases, current-turn deduplication, stall recovery, and current media/tool-result preservation; replaying the old session-row patches would fight the current invariants. | Current compression/state architecture. |
| FC-26 | External skill cache invalidation and bounded/full skill descriptions | I032, I103, I104, I112 | `DROP_UPSTREAM` | Current skill discovery and slash catalogs rescan external skills and current schema/prompt paths own description budgets; legacy literal limits no longer define the contract. | Current skill discovery and command catalogs. |
| FC-27 | Model-specific bounded execution guidance | I036 | `DROP_UPSTREAM` | Current system/developer prompt policy already supplies model-agnostic execution discipline, mandatory tool use, prerequisite checks, and verification without per-model prompt forks. | Current stable system/developer prompt. |
| FC-28 | Historical dependency/CVE pin stack | I042, I043, I061, I067, I073, I074, I075, I076, I077, I097, I109, I131, I151, I154 | `DROP_UPSTREAM` | v0.21 has a regenerated `uv.lock`/npm lock graph and an explicit upper-bound/SHA pin policy. Historical lock resolutions and one-off CVE pins must not be replayed across the new graph. | Regenerate from current manifests only. |
| FC-28B | Tornado 6.5.8 security bump | I155 | `DEFER_REPRODUCTION` | The preserved staging tip moves Tornado from 6.5.7 to 6.5.8 for two named advisories, while the reconstructed v0.21 lock still resolves 6.5.7. The old lock hunk cannot be replayed without fresh advisory and resolver evidence. | Run a current advisory/resolver qualification, then regenerate `uv.lock`; Critical dependency approval required before migration. |
| FC-29 | Disable scheduled Dependabot updates for GitHub Actions | I037 | `DROP_UPSTREAM` | Current `.github/dependabot.yml` deliberately enables weekly SHA-pinned Actions updates while keeping source dependency bumps manual; this intentionally supersedes the fork-local opposite policy. | Current repository security policy. |
| FC-30 | Fork OSV/process-killer CI and main-baseline checks | I038, I041, I127 | `DROP_UPSTREAM` | Current v0.21 CI/security workflows and classifiers have evolved substantially; the only fork-specific hosted gate still required—staging push coverage—is retained separately as FC-45. | Current CI workflows plus FC-45; do not replay stale workflow files. |
| FC-31 | Fork author attribution mapping in release notes | I039 | `DROP_LOW_VALUE` | No current reconstruction or release workflow depends on the private author mapping; current contributor audit derives authorship and salvaged credit from Git/PR evidence. | Use current contributor audit; add mappings only when a real release attribution fails. |
| FC-32 | Parallel pytest basetemp and profile-aware fixture/runtime fixes | I040, I043 | `DROP_UPSTREAM` | Current `scripts/run_tests.sh` runs test files in isolated subprocesses with temporary homes and CI-parity environment handling. | Current test harness. |
| FC-33 | Package-similarity/Tirith finding filtering | I063, I064, I066 | `DROP_UPSTREAM` | Current skills-guard and security-audit paths have since been redesigned and hardened; old warning-string suppressions are not the current behavior contract. | Current security audit/skills guard. |
| FC-34 | Legacy `FORK_FEATURES.md` inventory | I062 | `DROP_UPSTREAM` | The reconstruction now has source-ref preservation plus this complete v0.21 disposition ledger; restoring the old snapshot would create a second, stale authority. | `docs/fork/*` ledgers. |
| FC-35 | Request-dump/capture artifacts and human-review formatting | I044, I089, I102, I105, I106, I107, I108 | `DROP_LOW_VALUE` | The plugin/core patch stack is absent and read-only config has `request_capture.enabled: false`; it duplicates current observability/diagnostics while increasing prompt/PII retention surface. | Use current observability and explicit debug tooling; any future capture must be opt-in and external. |
| FC-36 | Audited multi-profile gateway restart model tool | I047, I072, I120, I128 | `REWRITE` | The legacy plugin is absent, but read-only config enables `gateway-restart-tool` for both Default and Ang. v0.21 now has a more capable drain/restart core, so the old implementation cannot be replayed unchanged. | Rewrite as a profile/session-gated plugin using the supported restart seam; Critical lifecycle approval required before implementation or release. |
| FC-37 | Hindsight history reconstruction command | I054 | `REWRITE` | Read-only config uses Hindsight as the memory provider and enables `hindsight-history`, but the legacy plugin is absent. Current provider/plugin interfaces differ from the old code. | Rewrite as a standalone profile plugin/CLI against the current memory-provider API; privacy approval required. |
| FC-38 | Cron Calendar lifecycle relay to Ops | I135 | `DROP_OUT_OF_SCOPE` | The relay targets external operational coordination and inherits the disabled Calendar consumer; Ops owns operational tooling and status relays. | Ops-owned automation outside Hermes source. |
| FC-39 | Google Workspace capability broker | I136 | `DROP_OUT_OF_SCOPE` | This is a user-specific external SaaS broker with its own policy/process surface and no current repo consumer; third-party products do not belong in Hermes core or bundled plugins. | Standalone external plugin/service owned outside this repo. |
| FC-40 | Stream full backup archives to stdout | I138 | `DEFER_REPRODUCTION` | The recovered patch is absent and no current usage evidence requires pipe-mode backups. It touches archive integrity, error-channel separation, and partial-output semantics. | Reproduce against current `hermes_cli/backup.py`; if retained, keep inside the backup CLI with no live-state writes during proof. |
| FC-41 | Gateway `/new (<prompt>)` shorthand | I139 | `DEFER_HUMAN_VALUE` | No current usage evidence establishes value, while the change spans destructive confirmation, active-session interruption, command parsing, title compatibility, and platform delivery. | Brian must confirm the interaction is wanted before any gateway-core design. |
| FC-42 | Private `/log` journal capture and nightly batch | I141, I153 | `DEFER_HUMAN_VALUE` | The plugin is absent and not enabled in read-only config. Privacy, retention, and usefulness are subjective; the duplicated branch commits do not create independent value evidence. | Brian decision first; if approved, isolated profile plugin plus cron skill/job, never core. |
| FC-43 | Block Personal History Log retains in Hindsight | I142 | `DEFER_HUMAN_VALUE` | The target journal/history workflow is not active, and a content-name-based retain gate is privacy policy rather than a generic memory invariant. | Brian must define the data boundary; then implement in a profile memory-policy plugin, isolated from FC-42. |
| FC-44 | Inactive release-switch controller | I143, I144, I145, I146, I147, I148, I149, I150 | `DEFER_HUMAN_VALUE` | The controller was explicitly inactive and v0.21 now has richer update, drain, restart, and recovery machinery. Whether Brian wants a separate human-authorized staging-to-main controller remains a product/operations decision. | Brian decision first; if retained, isolated Critical release-control service with explicit approval and rollback proofs. |
| FC-45 | Run hosted CI on staging pushes before main promotion | I152 | `KEEP` | Retained and verified in the first reconstruction batch; it is the current fork's authoritative hosted qualification gate. | Existing CI workflow trigger; checkpoint `brian-rebuild-v0.21.0-ci-staging`. |
| FC-46 | Recovered incident-evidence closure utility | I156 | `DROP_OUT_OF_SCOPE` | Hermes does not create or own incident-evidence directories; prior focused review assigned lifecycle tooling to Ops. | Ops-owned incident tooling. |
| FC-47 | Backup scan banner reports the actual archived root | I157 | `KEEP` | Retained and verified in the first reconstruction batch; behavior changes display only, not archive scope. | Existing backup CLI display path; checkpoint `brian-rebuild-v0.21.0-backup-scope-display`. |
| FC-48 | Tavily-specific quota fallback | I132 | `DROP_UPSTREAM` | v0.21 has generic one-shot keyed-backend rescue and deliberately removed Tavily; restoring the provider-specific arm would revive deleted coupling. | Current generic web-search rescue. |
| FC-49 | Background process completion notifications are opt-in | I129 | `DROP_UPSTREAM` | Current terminal/process tool contract defaults user-facing completion notifications off and requires explicit `notify_on_complete=true`. | Current terminal/process tool schema and runtime. |

## Ordered follow-up backlog

Historical reference only: these ten boundaries were created in `triage` by the old lane and remain incident-held with t_6d9a0f05 and all generated descendants. Do not release, execute, or auto-decompose them. Recovery task t_99b38c75 is the sole new writer in its own linked worktree. Human-value-deferred clusters FC-11 and FC-41–FC-44 require Brian's decision, not automatic migration.

| Order | Card | Cluster | Gate |
|---:|---|---|---|
| 01 | `t_11e2267d` | FC-01 structural config patch reproduction | Standard engineering review |
| 02 | `t_996fce1b` | FC-02 worktree-safe installer reproduction | Standard engineering review |
| 03 | `t_e5d7bfc4` | FC-03 IPv4-only browser reproduction | Standard engineering review |
| 04 | `t_23f26ea4` | FC-07 Langfuse path-like payload migration | Privacy/observability review; no enablement change |
| 05 | `t_49dacc47` | FC-16 Discord outbound channel authorization | **Critical authorization approval** |
| 06 | `t_48134fa4` | FC-22 Kanban hold/reclaim state safety | **Critical state/lifecycle approval** |
| 07 | `t_aa02a442` | FC-28B Tornado 6.5.8 qualification | **Critical dependency/security approval** |
| 08 | `t_45bddb6b` | FC-36 audited Ang gateway restart rewrite | **Critical lifecycle approval** |
| 09 | `t_536f175d` | FC-37 Hindsight history rewrite | **Critical privacy approval** |
| 10 | `t_e764b99f` | FC-40 backup stdout reproduction | Standard engineering review |

## Protected source-ref manifest

All exact protected refs used to build the union:

| Ref | Tip |
|---|---|
| `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | `2945588a014543d47c9e5e4a0d92ba6e361387c1` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/origin-main` | `2c554c568902fe74bf5d77ceda34068a7422e215` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/origin-staging` | `07677f36f5f005f2521dadc926b7e7d572acd760` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/recovered-wt__t_0c4486a1` | `0fb8e78acfa88cd462adcc9cab6f9785b3e319cf` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/recovered-wt__t_817455a6` | `2581b9cd8c0f504a4ff10a4207504b98e25af7db` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/release-commit` | `29112bef099274229cadff79cdff7bf7b99c4b77` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/release-tag-object` | `6e8f8418e6378eb2617e4de074e13dedd091b8af` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/staging` | `07677f36f5f005f2521dadc926b7e7d572acd760` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-fix__kanban-ready-integrity` | `25df328f8a27f80a2b04312c9b6e4e28fa4310f0` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-fix__kanban-worker-reclaim-safety` | `738e4a093a050f5a075bbbcae57db2ca98b3fa1d` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-fix__security-managed-venv-2026-09` | `75a40f9503f07da3a8e519ec532a7dc85bc90aef` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-fix__tornado-6.5.8` | `07677f36f5f005f2521dadc926b7e7d572acd760` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-main` | `2945588a014543d47c9e5e4a0d92ba6e361387c1` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-staging` | `07677f36f5f005f2521dadc926b7e7d572acd760` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_0086da14` | `2945588a014543d47c9e5e4a0d92ba6e361387c1` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_01f1e8fa` | `8c2b17a0dc0a6ad8cc5557b6fa834873ea813e71` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_0c4486a1` | `2945588a014543d47c9e5e4a0d92ba6e361387c1` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_132d2ba8` | `366a66d266306de0cb91c5fb9db14fceeea4a919` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_16cc80ba` | `2945588a014543d47c9e5e4a0d92ba6e361387c1` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_277d82ac` | `8bedb6fea8b721465cf7068433490ad371ea0c1a` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_2d5377a6` | `4c40304a96aa4fff2b6a57fce7fd8ceeb80e7097` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_3f347ffe` | `e6fab7b8e1d37a30821a81bd8f0329910edd18b7` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_5211e5ce` | `4d93606e520e6c9755813876a49c4f5c92404df6` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_757e3e93` | `1008d1fdbc5b0d27d4df17dfeb3abf9b80dcbce9` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_817455a6` | `2945588a014543d47c9e5e4a0d92ba6e361387c1` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_96ba6ce8` | `d509b7fb678f8292bee6117143f04677a9a01870` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_af7f24ea` | `6c120b4a4475a2ee3779a2f1eb92492ecfe18850` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_bd621e31` | `28b065d32e8a513d376db485c682fbd4fe4237b7` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_c8b3ecc9` | `a485633e0b7dfaa3fadf8bb438d538dbac5d7e6b` |
| `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_df3b3320` | `cd560d7dab22d2a73f197d9fd2f28ba9651de4eb` |

## Complete commit coverage appendix

A commit may name multiple clusters only when its diff was split by behavior. No item is omitted; no item is duplicated as a separate inventory row.

| Item | Commit | Exact preserved source ref | Cluster(s) | Subject |
|---|---|---|---|---|
| I001 | `e024744b1adaf25ca92e531c6cc0886d409f04b7` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-01 | feat(config): add concurrency-safe structural mutations |
| I002 | `d63dd88d258e366006fc344f60cd279284e35904` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-02 | fix(install): prevent worktrees from replacing the launcher |
| I003 | `445c814bac3f30c6534744903a005919b681b329` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-03 | fix(browser): tolerate unavailable loopback families |
| I004 | `d07ed4f8f26917bb004dd5b3e589a57ae7d1b137` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-04 | feat(tts): add configurable ElevenLabs voice settings |
| I005 | `7cf53fe45983e5373345ee11fca7e66208c3eb58` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-05 | feat(memory): decouple Hindsight from the file memory toolset |
| I006 | `0241eb894acab30e540f158c2c2b7d8faf524da4` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-06 | feat(tool-search): add platform-scoped pinned toolsets |
| I007 | `fce5c418c4bb64f06855b0d1fbde0065af26b085` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-07 | fix(langfuse): neutralize path-like text payloads |
| I008 | `7a54c4f9c6c3562e090242866beba869b2aacb09` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-08 | fix(disk-cleanup): prune wildcard matches recursively |
| I009 | `68fd6df29844e73255b6976a79338f57575da98d` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-09 | feat(webhook): add Google Pub/Sub OIDC authentication |
| I010 | `6108023a95b372577ab96f6c4b1806f98660c3c5` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-10 | feat(webhook): add guarded local script triggers |
| I011 | `be1d00981b3bfad3ed4615c7e8ea8a297ed28748` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-11 | feat(cron): add generic job lifecycle hooks |
| I012 | `dc9b2a2091a188930f7dca1d19e85028827ee739` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-12 | local(cron): refine delivery alerts and thread warnings |
| I013 | `00bbb91d7a1188bc2a206482133eb2f9c627ae44` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-13 | feat(cron): support file-backed prompts across tool and API |
| I014 | `cf6cf763ba4af9f8e8bc1540dcb6a7ff12389aa4` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-11 | refactor(cron-calendar): route output through COMPLETE hooks |
| I015 | `636486e1f56dacbed734cf9351bd525f1f6ed9f7` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-14 | fix(discord): escape markdown in outbound prose |
| I016 | `5767e5dc877491e43deb89c5565fe018d5df0a32` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-15 | local(discord): allow free-response channel auto-threading |
| I017 | `b519aa4389ac454534976fe7db1dd89f71185596` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-15 | fix(discord): retry failed slash command sync fingerprints |
| I018 | `6551554f91f2601f48f4469cf5c4f53f37f15cd3` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-24 | feat(discord-voice): add configurable STT aliases |
| I019 | `634d96a7ec95913aa749d16ed929a8ea6b60b085` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-16 | feat(discord): enforce channel policy on outbound sends |
| I020 | `6a5ce8913f8f72fca91830caab21e5181d6ca2f3` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-14 | fix(gateway): treat only standalone MEDIA lines as directives |
| I021 | `f11ca377e79c8ec43cbd02a3d56efa137ee145c7` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-14 | fix(display): abbreviate tool-call output in gateway displays |
| I022 | `c47b4668c6e452bf029dd867f49b76c5ea6f64a2` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-17 | fix(profiles): pin gateway cron and model switches to profile home |
| I023 | `f7c7bdf6307346350e5c5845a6e1ec954ae45f0a` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-18 | fix(kanban): report real per-board DB paths in board inventory |
| I024 | `55dd6e921b055db9ce94eb90d8fc446cc6d2185d` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-18 | feat(kanban): add card branch metadata |
| I025 | `8d8db3acb7e4925c5797371d6a8b6f095115edd1` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | feat(kanban-mirror): add Discord mirroring and reply routing |
| I026 | `5ff4cccf24d471503e0c6e3e18ec154e89530595` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-19 | feat(kanban): add notification routing policy |
| I027 | `c5665e35e00f7f86a4656cba3883ded561b994e3` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-24 | feat(discord-voice): add profile-scoped I/O and orchestration |
| I028 | `5feace5be2c15f07e5a9fa32ed7636bb3042fa0c` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-17 | fix(discord): pin durable adapter state to profile home |
| I029 | `83fd737806de9c6d4cce5ce4a27f869967e507ef` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-25 | fix(compression): preserve spoken voice context |
| I030 | `ed3b4e81bb40d9e1b4226bbb183bc2e5381e9984` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-25 | fix(compression): replace stale protected summaries |
| I031 | `e8488361a2912e186aaa35377262c6b26c231de7` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-25 | fix(compression): make manual rotation commit atomically |
| I032 | `b935eceaf8b50ef0c6dba7395b2667c27c861cd8` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-26 | fix(skills): invalidate prompt cache when external skills change |
| I033 | `5ce7243d0f305e38feed230d900ce1649173b013` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-19 | fix(kanban): log notifier tick diagnostics |
| I034 | `b08a62a9cb98323c44d02272133242c0f1731a0c` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-10 | fix(webhook): serialize script triggers per route |
| I035 | `b11a4a6d5708b9657e96fbf0d49dfd49329099d3` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-10 | fix(webhook): scope multiplex delivery state by profile |
| I036 | `e4344218855f790e190a7c1eb580f893916f7aea` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-27 | local(prompt): use bounded execution guidance for selected models |
| I037 | `2e516c9513206f60aee2b4c958afbd0a7593dc58` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-29 | local(deps): disable fork GitHub Actions update PRs |
| I038 | `1a32065b6c1ff8648ec15fb4721e32e242cb59ab` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-30 | local(ci): keep fork OSV scans active |
| I039 | `98e9b8005bf2dd3a813bc7040256edf7c610b871` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-31 | local(release): map fork author attribution |
| I040 | `5f9c82e30b772ad23dbfff66faeb11b711da1ac2` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-32 | fix(tests): isolate parallel pytest basetemps |
| I041 | `903a22741f9a0d2fa38cf3c18557d1b5d7bb3e7d` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-30 | fix(ci): distinguish process-killer commands from arguments |
| I042 | `70adc9bec2d320c231c84b4947d3519785ed5fc8` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-04, FC-28 | fix(tts): preserve display stream without premium SDK |
| I043 | `66c2bcf848077464e959feca21bab1baa592b20c` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-28, FC-32 | fix(ci): align profile-aware fixtures and runtime dependencies |
| I044 | `c1cce48f703aad80d191e8b2e8fcaab3b2373cab` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-35 | feat(request-dump): add request-context estimate command |
| I045 | `f2a84c5936c5180f8b1e6f3533aed497d3d5e41e` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-20 | fix(kanban): scope worker guidance to task sessions |
| I046 | `0cf7d3dea6ca2afeee423188d0a4575cd6aa8c9c` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-11 | feat(cron-calendar): add calendar sync plugin |
| I047 | `334acf3f93a8bbe58955807a00cbe081aa73e2e4` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-36 | feat(gateway-restart): add multi-profile restart plugin |
| I048 | `e9b448b2c7f570a4b65781b96524200a12208518` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-24 | feat(discord-voice): add command acknowledgement flows |
| I049 | `ffce6eed74a49905b70f0b59656592870f2cf4b3` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-24 | feat(discord-voice): stop TTS playback with /stop |
| I050 | `50c0f59b86de88dfa15e234d993e1ea56629d088` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-24 | feat(discord-voice): normalize fillers for voice commands |
| I051 | `d71f3509479b6cd84e880d3432de0ffdfd57ab56` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-24 | feat(discord-voice): add profile-scoped acknowledgement catalogs |
| I052 | `38a2d27bd702fdac610d3b9449a266b25483bc91` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-17 | refactor(gateway): remove shadowed adapter resolver |
| I053 | `e1530115af56c0b746f20383c8436e47d7494cb6` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-24 | feat(discord-voice): move STT aliases to the profile catalog |
| I054 | `d0b90d532f347a40f7ddc78aa223492272c3ff99` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-37 | feat(hindsight-history): add history reconstruction command |
| I055 | `ba6316148dfc6ff70f9f82072c80ee7b983df1ad` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | feat(kanban-mirror): add durable Discord lifecycle and recovery |
| I056 | `36a1dcedd618cf924d3efe3ad1a9b1230ae74af9` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | refactor(discord): move Kanban mirror into platform plugin |
| I057 | `ad4b57478df3b935a9f0b08fb6f631a82bef658f` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-11 | feat(cron-calendar): bring plugin to Ops parity |
| I058 | `5b6a3cab14c473678573b1fa9e76eb718e1464ce` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-11 | fix(cron-calendar): restore CI discovery and mixed-timezone matching |
| I059 | `b488245ddbd0a3a870a6e7e5299d9e6768bc3b98` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-24 | refactor(discord-voice): move runtime orchestration into platform plugin |
| I060 | `6b030c1085570de205a3cfeaaa186c2dacc42798` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-18 | fix(kanban): validate persisted workspace and branch metadata |
| I061 | `2aea1d07d3734ff2d5473c0dbc6f17ca3ab01a7f` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-28 | security: update DOMPurify past GHSA-c2j3-45gr-mqc4 |
| I062 | `39a0235752a482e7f12ff49ec2ce9a3ab31bb824` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-34 | docs: inventory fork features and configuration |
| I063 | `73d88f7aa7e246811ffb4df4274869298ccb62e1` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-33 | fix(security): suppress exact package similarity warnings |
| I064 | `2f029291dc1f4851db971915391ba2e53570606f` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-33 | fix(security): retain distinct package similarity warnings |
| I065 | `b7a08a7a37dbd05437e90f35d895a10bb73250ef` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-17 | fix(gateway): scope service PIDs to active profile |
| I066 | `da56996649cf498ebad0879badb256d53d2868a6` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-33 | fix(security): filter Tirith findings before display cap |
| I067 | `8e6f96e2b75a3ed6e6650513679b61db262d39c8` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-28 | chore(deps): refresh Python lock resolution |
| I068 | `9e811f14029061384c6af793c229ee2085e8d297` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-17 | fix(discord): scope channel allowlists by profile |
| I069 | `557630d866486b0ae1589569fd3bf3636aad1e99` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-17 | feat(gateway): multiplex profile runtimes safely |
| I070 | `ded34ba962d7022e2b12552591191aaef37618c4` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-17 | fix(gateway): stabilize multiplex runtime health |
| I071 | `660ad601b19f78e5ab0632872cb1d7b1b68b9454` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-10 | fix(webhook): reap timed-out script process trees |
| I072 | `c11e8867215cbf998bef73871dd3eeb17a49f798` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-36 | Simplify gateway restart to one process-wide tool |
| I073 | `352b649fb43b22a6566bb3e96ec9ef15219d5c47` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-28 | security(deps): update Python advisory pins |
| I074 | `ebdfb4803c2c77ca7783d2f9ea44d7fb39c03dcf` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-28 | security(deps): enforce ASGI pins in core installs |
| I075 | `05ebad7f277b3e05fcb0edbef8b3d18a6cd31bbb` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-28 | Merge security dependency pins |
| I076 | `bdf574552ea68dd89a186d8666057c760d0480b0` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-28 | security(deps): update pytest and setuptools advisories |
| I077 | `f8d1ad9bcd7779f5e2fe3f8faa4e538258c21bd7` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-24, FC-28 | security(discord): carry patched PyNaCl voice dependency |
| I078 | `866e289a7179f05a2f0b36c8bdb2f1ac159597e8` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-14 | fix(gateway): require standalone extensionless media directives |
| I079 | `14c74837aa279337be55dfe7daec0fda7f8bb7b6` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-17 | fix(discord): scope ignored-channel policy by profile |
| I080 | `b0f953af0de58e71ef796dc610d802656542ef01` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-19 | fix(kanban): fail closed on unknown notification policy |
| I081 | `4bcbe344a4e8fd2606073b8b8c06c482698ecd4b` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): repair mirror bindings and lifecycle recovery |
| I082 | `a7e90e03a853e80d10434b947dd29aefec95baf2` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): distinguish redirected mirror edits |
| I083 | `c0c4802c64fd3627c9e6986f87f0e96b93231e76` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): recover bindings before reconciliation |
| I084 | `536f177831c4130b3fc274262f00a2c7812eada3` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): converge stale mirror lifecycle state |
| I085 | `aad5b839efda2ed8bb317c4033acfa9cbfc3fd93` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): fail closed on conflicting inbound recovery |
| I086 | `c154c17049ce65b06f431fe4a265e5d6e4942553` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): harden mirror state convergence |
| I087 | `d200ca5b3320e5917e608c733869c7dc56c9c6d9` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): close sibling quarantine recovery races |
| I088 | `2a21ec85b4ef2cd61f9b1d20f63945853cfa0718` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): bind successor cleanup to transition epoch |
| I089 | `2a75940cee48bbf873de78ca7acc124fd2a92900` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-35 | local: format request captures for human review |
| I090 | `2e33cdb87389f141c70762e0c9809b4e87ab3687` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): cancel all remaining thread work |
| I091 | `31512cfa6edd07fb71f0dc1d55c3f296fa39844c` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): scope thread cancellation to board |
| I092 | `0491039f70a09e7d72cc1dc6644d586e841a5633` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): block cancellation with queued directives |
| I093 | `1d59489c5a80c63928547dd83d6f07beef1738d8` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | merge: deploy Discord thread cancellation |
| I094 | `8d8e26a5f82e1fc02358bfcd2e90fc570ae92a31` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): close stale threads after cancellation |
| I095 | `cefab9eb29d20a3b3eb8ba86d8d0590918d6d42a` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): rearchive orphaned done threads |
| I096 | `5859c9124f0d2d45b2ce6d48b7e056ec4ce07449` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-17 | fix(gateway): preserve multiplex adapter profile provenance |
| I097 | `567f2bed231a228e5b7ac42c0d11af0b5e62f2b8` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-28 | security: update PostCSS past source-map disclosure advisory |
| I098 | `2e458684c3eae41d44794ccda8f4287ebe349b05` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-17 | fix(gateway): preserve shared route transport adapter |
| I099 | `b6bf7b2fd409e58857a5c2035ef1582f0be83c8d` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-17 | fix(gateway): scope pairing approval to transport profile |
| I100 | `277bca56d82b41ab13de3af5f0c6f9c9eac3244b` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-17 | fix(gateway): scope pairing flow to transport profile |
| I101 | `4510c9d532920d17758144aa95dd066a74d3b166` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-17 | fix(gateway): isolate multiplex pairing policy |
| I102 | `a3c2961406fa9e9625a6c57142403610d7353608` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-35 | local: wrap long request capture values |
| I103 | `30a9eb22dd5d7ce1557863cc79cc5639761491a6` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-26 | local: preserve full skill descriptions in request context |
| I104 | `701508b388eacd8e0c6d4196825704cba83f5425` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-26 | local: align skill description limit with schema |
| I105 | `c712069aceb68ebbd3cdf7df6049899060c3d2d1` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-35 | local: summarize request capture context footprint |
| I106 | `c02cf84e82181be584fb32cd7b09c23eb5bfc358` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-35 | local: expand request capture artifact set |
| I107 | `6d47b4d7c394364856ed9d51879681c47718e619` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-35 | local: keep raw request captures metric-free |
| I108 | `c1812274fd1312628965779792d8ff793e6a14a9` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-35 | local: retire superseded system prompt dump plugin |
| I109 | `4b301fb819a50c44ea887f125f8ce4f5e7091127` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-28 | fix(security): upgrade React Router past RSC XSS |
| I110 | `a5c25a69ba4ac1857a7a5aaf3feb4893973fd501` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-11 | feat(cron): publish atomic lifecycle events |
| I111 | `d89e183e8d6218c984490492f711f168e02b10aa` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): keep mirror repair diagnostics out of threads |
| I112 | `82bc9dc93d57ecf5769bcc7b35c2d8980fee73bb` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-26 | fix(skills): honor small description limits |
| I113 | `17b301870141ec8cc3f86c5c25e40629c629ca8f` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-19 | fix(kanban): fail closed on malformed notification policy |
| I114 | `9bf1becad59d6f4b589f399cee4d779f18ecb6ea` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): preserve unroutable cancellation directives |
| I115 | `754c7bd8301d414aeea8832d3bd8597b6a07be4d` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): replace stale workflow tags on completion |
| I116 | `b24363e224e1587ae0ad6a76c240c129b9750c02` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): reject unsafe mirror profile overrides |
| I117 | `14f078e6ba50dd07c708e86112fc3d5f18774bb3` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): audit duplicate Kanban threads during reconciliation |
| I118 | `4c0bafabc16c3c1fa7ad59dc45d454a9fd5e0c85` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): release resolved mirror quarantines |
| I119 | `67de4561157ea5aa72c3dbe215320b47b1ecac50` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): reconcile Kanban mirror lifecycle |
| I120 | `2f630ed31d47a5a7cd12f162be2024b92bc64cb1` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-36 | fix(gateway): deny restarts while agents are active |
| I121 | `1a2517b448f8d1d7950b6f80a527289531b7a1b6` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-17 | fix(discord): preserve explicit bot profile route |
| I122 | `732b14dfa80d898abe268a9314a4bcf2397454fe` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-23 | fix(discord): authorize gated Kanban reaction decisions |
| I123 | `ee25e228691bf97aab30064b64fe1b4236e4d834` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-21 | feat(kanban): add Human Action Brian Queue |
| I124 | `de48c0c0a876ef8d0916ad85bda1e6630dc751ac` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-11 | feat(cron): unify Calendar lifecycle reconciliation |
| I125 | `9cf1e220a2c45d538ea33d39d640fb96d40be515` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-20 | fix(kanban): isolate delegated children from parent lifecycle |
| I126 | `8b7b69b67b4410d366c0d2b8e3c082332e47aa2f` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-17 | fix(gateway): isolate session routing by profile |
| I127 | `f618aecf8735ca4883b52a04fc7d372911be18aa` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-30 | fix(ci): restore main baseline checks |
| I128 | `2c554c568902fe74bf5d77ceda34068a7422e215` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-36 | fix(gateway): make restart tool drain-aware |
| I129 | `2945588a014543d47c9e5e4a0d92ba6e361387c1` | `refs/backup/hermes-v0.21.0-precleanup-20260903/main` | FC-49 | local: make background completion notifications opt-in |
| I130 | `25df328f8a27f80a2b04312c9b6e4e28fa4310f0` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-fix__kanban-ready-integrity` | FC-22 | fix(kanban): keep operator holds out of ready queue |
| I131 | `1008d1fdbc5b0d27d4df17dfeb3abf9b80dcbce9` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_757e3e93` | FC-28 | [verified] security: refresh web and tui lockfile dependencies |
| I132 | `8bedb6fea8b721465cf7068433490ad371ea0c1a` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_277d82ac` | FC-48 | fix(web): fall back when Tavily quota is exhausted |
| I133 | `4d93606e520e6c9755813876a49c4f5c92404df6` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_5211e5ce` | FC-11 | fix(cron): recover stale Calendar output instances |
| I134 | `366a66d266306de0cb91c5fb9db14fceeea4a919` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_132d2ba8` | FC-08B | fix(disk-cleanup): preserve durable profile scripts |
| I135 | `28b065d32e8a513d376db485c682fbd4fe4237b7` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_bd621e31` | FC-38 | fix(cron-calendar): relay lifecycle events to Ops |
| I136 | `4c40304a96aa4fff2b6a57fce7fd8ceeb80e7097` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_2d5377a6` | FC-39 | feat: isolate Google Workspace capabilities behind local broker |
| I137 | `738e4a093a050f5a075bbbcae57db2ca98b3fa1d` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-fix__kanban-worker-reclaim-safety` | FC-22 | fix(kanban): defer max-runtime reclaim for live workers |
| I138 | `d509b7fb678f8292bee6117143f04677a9a01870` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_96ba6ce8` | FC-40 | fix(backup): stream full archives to stdout |
| I139 | `8c2b17a0dc0a6ad8cc5557b6fa834873ea813e71` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_01f1e8fa` | FC-41 | fix(gateway): deliver /new parenthesized prompt |
| I140 | `cd560d7dab22d2a73f197d9fd2f28ba9651de4eb` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_df3b3320` | FC-17 | fix(gateway): scope standalone delivery mirrors by profile |
| I141 | `a485633e0b7dfaa3fadf8bb438d538dbac5d7e6b` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_c8b3ecc9` | FC-42 | feat(journal): add private slash capture and nightly batch |
| I142 | `e6fab7b8e1d37a30821a81bd8f0329910edd18b7` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_3f347ffe` | FC-43 | local: block Personal History Log Hindsight retains |
| I143 | `2288bb96739ccb59d92f6ba896b890ecb25f3197` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_af7f24ea` | FC-44 | Add inactive Hermes release switch controller |
| I144 | `c7e8b48e6b1131365a7648eb8834b0da16c9708f` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_af7f24ea` | FC-44 | fix release controller safety gates |
| I145 | `04797620edc6998e201c86cb3333e1a22b30e6bb` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_af7f24ea` | FC-44 | fix(release): harden controller preflight gates |
| I146 | `016dad23447440ca821a9f46d3130e3d44f833f8` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_af7f24ea` | FC-44 | fix(release): harden staged promotion recovery |
| I147 | `25d546bccc09ff5acad3ae2f561cc9230344e2b7` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_af7f24ea` | FC-44 | fix(release): bind stopped runtime and approval proofs |
| I148 | `3cee339e55baad270283bb5c11faaecf3a18dbec` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_af7f24ea` | FC-44 | fix release promotion recovery gates |
| I149 | `baa909b01a2a1b5890e8a3ec214c72be68d385c6` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_af7f24ea` | FC-44 | fix release promotion switch crash recovery |
| I150 | `6c120b4a4475a2ee3779a2f1eb92492ecfe18850` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-wt__t_af7f24ea` | FC-44 | docs(release): use python3 in sandbox walkthrough |
| I151 | `75a40f9503f07da3a8e519ec532a7dc85bc90aef` | `refs/backup/hermes-v0.21.0-precleanup-20260903/worktree-fix__security-managed-venv-2026-09` | FC-28 | fix(deps): remediate managed-runtime CVEs |
| I152 | `597d7030585eb574e30b40eb44c11764f79f7891` | `refs/backup/hermes-v0.21.0-precleanup-20260903/staging` | FC-45 | ci: validate staging before main promotion |
| I153 | `f82c6922a076ac16a35b87013e5e6450f4c4360a` | `refs/backup/hermes-v0.21.0-precleanup-20260903/staging` | FC-42 | feat(journal): add private slash capture and nightly batch |
| I154 | `28e720df90a79c55381f94ad4f78457c16a4025d` | `refs/backup/hermes-v0.21.0-precleanup-20260903/staging` | FC-28 | fix(deps): remediate managed-runtime CVEs |
| I155 | `07677f36f5f005f2521dadc926b7e7d572acd760` | `refs/backup/hermes-v0.21.0-precleanup-20260903/staging` | FC-28B | fix(deps): bump tornado to 6.5.8 |
| I156 | `0fb8e78acfa88cd462adcc9cab6f9785b3e319cf` | `refs/backup/hermes-v0.21.0-precleanup-20260903/recovered-wt__t_0c4486a1` | FC-46 | wip: preserve recovered incident-evidence closure utility |
| I157 | `2581b9cd8c0f504a4ff10a4207504b98e25af7db` | `refs/backup/hermes-v0.21.0-precleanup-20260903/recovered-wt__t_817455a6` | FC-47 | wip: preserve recovered backup scope display fix |

## Mechanical coverage proof

Regenerated 2026-09-06 on branch `brian/reconstruct-v0.21.0-recovery` at baseline `495b851fbc0b8b3531e56db5373b4bf4446ff288`:

- Protected refs: `30`; unique tips: `22`.
- Legacy-only union vs baseline: `157` commits; inventory SHAs match the union exactly (0 missing, 0 extra).
- Inventory rows: `157`; unique SHAs: `157`; merges: `2`.
- Covered ordinals: `1..157`; missing: `[]`; duplicate inventory rows: `[]`.
- Intentional mixed-commit splits: I042 → FC-04, FC-28; I043 → FC-28, FC-32; I077 → FC-24, FC-28.
- Cluster rows: `51`; disposition total: `51` (KEEP 8 after FC-02/FC-03 migrations, REWRITE 2, DROP_UPSTREAM 24, DROP_LOW_VALUE 5, DROP_OUT_OF_SCOPE 3, DEFER_HUMAN_VALUE 5, DEFER_REPRODUCTION 4).
- Inventory clusters ↔ matrix clusters: exact bijection; omission index and Brian-UAT index point only at matrix IDs.
- Manifest tip SHAs match live `refs/backup/hermes-v0.21.0-precleanup-20260903/*` with zero mismatches.
- Prior historical “Codex PASS” claim is **not** relied on. Native second-pass review sampled DROP/KEEP premises against current tree paths (examples: FC-05 Hindsight plugin present; FC-13 `prompt_path` present in cron tool/jobs; FC-17 multiplex/profile adapters present; FC-48 Tavily backend removed with generic rescue retained; FC-49 `notify_on_complete` defaults false; FC-07 Langfuse lacks legacy multiline absolute-path neutralization; FC-02 worktree launcher guard absent from current doctor/install; FC-03 `find_free_debug_port` still requires both loopback families and RED-reproduces on this IPv4-only host by returning an occupied `preferred+1`).
- Protected refs remain the durable source of truth; `/tmp/t_6d9a0f05-inventory.json` and `/tmp/t_99b38c75-recovery/` are supporting artifacts only.
