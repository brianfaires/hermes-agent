# v0.23.0 upgrade impact and dead-key baseline

Ticket: brianfaires/hermes-shared#58 (map #52). Revs: `v0.22.5`, `v0.23.0`, baseline `3d5a9416f2` (fork). Primary sources: upstream tag contents (`git show/grep v0.23.0:...`), `gh release view v0.23.0`, and read-only consumer evidence under `~/.hermes`. Key names, paths and counts only; no values.

Note: the live checkout (`~/.hermes/hermes-agent`, `main`) is at `86fed153b9`, 2 commits behind the baseline (#221, #222 not deployed). Findings below hold for either.

## 1. Verdict

No upstream change forces a rebuild-blocking break on its own. Four things need action regardless of which fork features survive:

1. Config v51 -> v52 migration deletes `browser.backend` (ops) and the old browser tools are gone; two skills and one ops health script still name them.
2. 28 of 265 distinct config key paths have no `v0.23.0` reader (section 6); 4 of them are read by the baseline.
3. Three user-code imports target fork-only modules that vanish with the fork code (section 4); they are dependencies of fork features, not upstream breaks.
4. Rebuild conflicts concentrate in 92 non-test files touched by both the fork (`v0.22.5..3d5a9416f2`, 268 files) and upstream (`v0.22.5..v0.23.0`, 5411 files); none of the fork-touched files is deleted upstream.

## 2. Release-note breaking changes vs this deployment

Source: `gh release view v0.23.0 -R NousResearch/hermes-agent`, "Upgrade notes".

| Change | Affects here? | Evidence / action |
|---|---|---|
| Built-in `browser_*` tools and agent-browser removed (`browser_exec` only) (#143146) | Yes, minor | Consumers: `skills-library/pools/research/monitoring/product-price-monitor/SKILL.md` and `.../research/grounded-citations/SKILL.md` name `browser_navigate`; `skills-library/lint_known.yaml` lists `agent-browser`; `doctor-remediation-patterns.md` mentions agent-browser npm; `profiles/ops/scripts/health/tool-checks/browser-smoke.py` dispatches `browser_navigate`/`browser_snapshot` (already ImportError-skips at baseline: `check_browser_requirements` lives in `tools/browser_tool_install.py`, so it is a warning-only no-op; on v0.23.0 the tools it dispatches are gone too). No cron job, webhook subscription, profile plugin or profile skill names them. `browser` toolset name still exists (`toolsets.py`), so `platform_toolsets.*: browser` entries stay valid. Action: edit the 2 skills to `browser_exec`, retire or rewrite browser-smoke. |
| `hermes mcp serve` -> `hermes mcp-serve` (plugin) | No | No `mcp_servers` key in any profile config; no systemd unit, cron job or script launches it. |
| Camofox, import-agent, Singularity leave core (auto-installed plugin) | Probably no | No `singularity`/`camofox`/`import-agent` strings in configs or cron. Row predicate is read-only; `_left_core_installed` (root, ops) already records `langfuse`, `spotify`. Open question 2. |
| Word/PowerPoint/Excel skills -> `microsoft-office` | No evidence | Bundled-skill change; old copies stay on disk. |
| Webapp off by default (`webapp.enabled: false`, port 9120 default) | No | Nothing to disable; no key in configs. |
| Electron 44 / Desktop OS floors | No | Desktop not deployed on this host. |
| Plugin auto-update (`plugins.auto_apply`, default off) | No | No profile sets `plugins.auto_apply`. |
| Remote config backend (`HERMES_CONFIG_BACKEND=remote`, opt-in) | No | Not set in the gateway unit. |
| Bundled skills 58 -> 44; 12 optional | Check | A profile that relies on a bundled skill moved to optional needs a reinstall; not enumerated here (skills ticket). |

## 3. Config schema and migrations

- `_config_version`: all 4 configs (root, ang, ops, rook) are 51. `v0.23.0` `DEFAULT_CONFIG` is 52 (`hermes_cli/config_defaults.py`); `v0.22.5` was 51. `SUPPORT_FLOOR_VERSION` stays 12. The fork did not edit `config_migrations.py` (`git diff v0.22.5 3d5a9416f2` is empty for it).
- Only one new step: `migrate_to_52` (`hermes_cli/config_migrations_browser.py`): removes `browser.backend` and `browser.extension_control`, and moves `AGENT_BROWSER_*` from `.env` into `browser.*`. Here: `browser.backend` exists in the ops config only; `.env` files contain 0 `AGENT_BROWSER_*`/`CAMOFOX*` entries (counts only); `browser.cloud_provider` (root, ops), `browser.allow_private_urls`, `browser.auto_local_for_private_urls` stay valid. The step is in `LEGACY_KEY_STEPS`, so it also runs on unversioned configs.
- `run_migrations(..., config_only=)` and `_local_files_allowed()` are new (remote-backend support); steps 13, 23, 41, 49 skip local-file work only in that mode. Normal runs unchanged.
- Defaults removed upstream `v0.22.5 -> v0.23.0`: `auxiliary.skills_hub.*`, `browser.backend`, `browser.camofox.*`, `browser.extension_control.*`, `browser.allow_unsafe_evaluate`, `browser.record_sessions`, `browser.restrict_evaluate`, `browser.snapshot_threshold`. 31 defaults added. Fork-added defaults (baseline minus `v0.22.5`): `skills.prompt_description_limit`, `tools.tool_search.pinned`; the second is not set in any profile.
- Fork edits to `hermes_cli/config.py` (248 lines), `config_defaults.py`, `config_effective.py` collide with upstream's restructure of the same files (`config.py` 137+/240-, new `config_backend.py`, `config_issues.py`, `config_runtime_keys.py`). Rebuild conflict expected; the fork-only `config_transactions.py` / `config_side_effects.py` (commit `6c3d9e74c1`) must be re-seated on `config_backend.read_config_doc`. Owned by the config-feature tickets.

## 4. Plugin, hook and tool API

Method: AST-extract every internal import (`hermes_cli`, `gateway`, `agent`, `tools`, `cron`, `hermes_constants`, `utils`, `plugins`, ...) from user code, then check module and symbol existence in `v0.23.0` and in the baseline. Scope A: `hermes-extensions/{plugins,commands}`, `~/.hermes/plugins`, `profiles/*/plugins` (120 distinct imports). Scope B: `profiles/*/scripts`, `~/.hermes/scripts`, `skills-library`, profile skills (28 distinct imports).

- Upstream `PluginContext` (now with `PluginCapabilityCtxMixin` in `hermes_cli/plugins_capability_ctx.py`): 0 methods removed or renamed vs `v0.22.5`; added `side_question` (consent-gated). `VALID_HOOKS`: 0 removed, 0 added vs `v0.22.5` (`post_compaction`, `on_session_delete` already existed). Release-note "new" hooks/APIs are additive.
- All hooks the user plugins register (`pre_gateway_dispatch`, `post_gateway_admission`, `pre_tool_call`, `post_tool_call`, `on_session_reset`, `on_session_end`) and all `ctx.*` methods they call (`register_hook/tool/command/cli_command/platform/platform_handler/memory_provider/system_prompt_section/skill`, `get_config`, `inject_message`, `spawn_task`, `on_unload`, `has_plugin`, `current_cron_execution`, `profile_name`, `_gateway_injection_allowed`) exist in `v0.23.0`. `plugins.entries.newprompt.allow_gateway_injection` is still read at `hermes_cli/plugins.py:632`.
- Broken imports, all against fork-only code (present at baseline, absent in `v0.23.0`):
  - `cron.execution_results.iter_execution_results` <- `hermes-extensions/plugins/cron-calendar-mirror/mirror.py` (fork commit `0419b1788b`).
  - `hermes_cli.config_transactions.config_mutation_lock` and `hermes_cli.config_side_effects.sync_config_changes` <- `hermes-extensions/commands/hermes_config_patch/__init__.py` (fork commit `6c3d9e74c1`).
- Fork-only hook `fallback_activated` is not in `v0.23.0` `VALID_HOOKS`. Consumer: `hermes-extensions/commands/fallback-alert/notify_fallback_alert.py` (shell hook, README documents `hooks.fallback_activated`) and `plugins/tests/test_high_priority_alert.py`. No profile config registers it (only `hooks.post_api_request` is set, in root, ang, ops), so no live registration breaks.
- Pre-existing (not v0.23.0): `scripts/hindsight/warm_hindsight.py` imports `plugins.memory.hindsight`, absent at baseline too.
- Plugin loading: every name in `plugins.enabled` (19 root / 8 ang / 16 ops / 4 rook) resolves to a bundled `v0.23.0` plugin or a user plugin; no enabled/disabled overlap (the new loader and `hermes doctor` check). User `plugin.yaml` keys are all known; `langfuse` has `requires_hermes: ">=0.21.7"`, satisfied. Plugin skills become `plugin:name` (#143822); not audited against profile skills.
- Pm plugin eviction (`pm/plugin_eviction.py`): `hermes update` now disables, rather than fails on, an enabled plugin that no longer fits; it judges `requires_hermes` against a version identity that can lag on a checkout without release tags. Rebuild branch needs the `v0.23.0` tag reachable, or evictions may sit out.

## 5. Packaging, launcher, gateway service

- `pyproject.toml`: `requires-python >=3.11,<3.15`, scripts (`hermes`, `hermes-agent`, `hermes-acp`) and `version = "0.0.0"` unchanged. Extras: added `documents`, `documents-pdf`, `keychain`; none removed. Many pins bumped (including `openai` 2.24.0 -> 2.54.0, `anthropic` 0.87.0 -> 0.125.0, `nemo-relay` 0.10 -> 0.11). Fork adds extra `scripts = ["PyYAML==6.0.3"]` (+ `pm/extras.py` map entry, `uv.lock`). PyYAML is still not a core dep in `v0.23.0` (core uses `ruamel.yaml`), so the extra is still needed by this host's maintained scripts. `uv.lock` must be regenerated, never hand-merged.
- Launcher: the checkout's `.hermes/bin/hermes` (what `~/.local/bin/hermes` and the unit exec) is unchanged in role; `hermes_cli/_launchers.py` change is Windows-launcher logic plus "a worktree/review tree started against the home leaves the installed `hermes` alone". `hermes pm install --without EXTRA` is new. `~/.hermes/tools` holds `agent-browser-0.26.0`, `chromium-1208`, `node-*`; v0.23's step 52 notes pm's own Chromium is now the default executable, so the agent-browser store entry is probably unused.
- Systemd (`hermes-gateway.service` + drop-ins `20-cron-timeout.conf`, `hardening.conf`, `hindsight.conf`): the unit generator in `hermes_cli/gateway.py` is unchanged in substance vs `v0.22.5` (same `ExecStart=... gateway run`, `ExecStop/ExecStopPost` modules `gateway.systemd_stop_mark`/`gateway.cgroup_cleanup`, `Restart=always`, exit 75 force-restart, 78 prevent, `KillMode=mixed`). Watchdog (`Type=notify`) remains opt-in and is off in the live unit. `HERMES_CRON_TIMEOUT` is still the cron inactivity setting (`cron/jobs.py`, `cron/env_settings.py`). Drop-ins are not touched by upstream. `hermes-web-failover-ops.service` runs a profile script, outside upstream.
- Service-manager changes in `v0.23.0` are s6/Docker, launchd and Windows paths; no systemd-user impact found.
- Behavior change to carry into gateway/cron tickets: "one gateway owns every conversation" (#106742, #140334): CLI, TUI, ACP and cron attach to the machine's gateway as clients (`gateway/runtime_ownership.py`, new `gateway.lock` probing). Fork gateway patches (`gateway/run*.py`, `session_*.py` are in the 92-file overlap) must be re-validated against it.

## 6. Dead-key baseline

Method (script in scratch, not the repo: `/tmp/claude-1000/-home-brian--hermes/ee70c9c0-811b-474a-866b-be7acbacdc27/scratchpad/dk/`): flatten every key path in `~/.hermes/config.yaml` and `profiles/{ang,ops,rook}/config.yaml` (265 distinct paths: root 225, ang 78, ops 207, rook 65); index quoted string tokens of every non-test `.py` file in `v0.23.0` (and separately in the baseline), plus user plugins and `hermes-extensions`. A key counts as read when its leaf and parent literals co-occur near each other (400 chars) or as a dotted literal in a file other than the default/config declaration modules. Dynamic segments (`model_aliases.<n>`, `plugins.entries.<p>`, `display.platforms.<p>`, `skills.config.<s>`, `platforms.<p>`) are ignored. Residual `LOOSE` matches (parent and leaf in the same file, not adjacent; 81 keys) were spot-checked; static analysis cannot prove those reads, but a miss is bounded to keys read through generic section dicts (adapters, `display_config`, `tools_config`). Non-obvious classes were hand-verified (see the evidence column); `DECL-ONLY` hits were confirmed read via dynamic access (`platform_hints`, `tool_loop_guardrails.*_enabled`, `web.extract_char_limit`) except `tts.elevenlabs.style`.

Result: 237 keys have a `v0.23.0` reader; 28 do not.

### 6a. Dead in `v0.23.0`, read by the baseline (dropped feature or removed upstream: resolve explicitly)

| Key | Profiles | Evidence | Disposition |
|---|---|---|---|
| `skills.prompt_description_limit` | root, ang, ops, rook | Fork commit `97455dd381` reads it (`agent/skill_prompt_settings.py`, `agent/skill_utils.py`). `v0.23.0` has only the fixed `SKILL_PROMPT_DESC_LIMIT = 60` (`agent/skill_utils.py:846`); key absent from defaults. | Live consumer. Restore with the fork feature, or remove the key with Brian's agreement (skills ticket). |
| `auxiliary.skills_hub.model`, `.provider` | root, ops | `v0.22.5` and baseline read it (`hermes_cli/skills_hub.py`, `skills_hub` aux task). `v0.23.0` `config_defaults.py:747`: "web_extract, session_search, skills_hub: no aux LLM any more, leftover blocks are ignored" (upstream `a285c4f4ba`). | Upstream removal. Remove keys. |
| `browser.backend` | ops | Read by `v0.22.5`/baseline; `v0.23.0` only deletes it (`migrate_to_52`). | Auto-removed on first run; remove now to keep diffs clean. |

### 6b. Dead in `v0.23.0` and in the baseline (never read by upstream or baseline; stale or fork-branch config)

| Key | Profiles | Evidence | Disposition |
|---|---|---|---|
| `auxiliary.web_extract.model`, `.provider` | root, ops | Already "leftover blocks are ignored" in `v0.22.5` defaults; absent from `v0.22.5` `DEFAULT_CONFIG`. Static tiers show a false LOOSE hit. | Remove. |
| `checkpoints.delete_orphans` | root, ops | Only a CLI flag/argument (`hermes_cli/checkpoints.py:69`, `tools/checkpoint_maintenance.py`); no config reader in any ref checked. | Remove. |
| `cron.calendar_sync.enabled`, `.calendar_id` | root, ops | No core reader. Read by user scripts `profiles/ops/scripts/cron_calendar_recurring_sync.py:103` and `health/tool-checks/cron-calendar-consistency.py:243` (not plugins, so outside the method's scope). The mirror plugin reads `plugins.entries.cron-calendar-mirror.settings.calendar_id` instead. | Keep; live user-owned consumer. Cron ticket decides if folded into plugin settings. |
| `cron.events.enabled`, `.directory` | root, ops | No reader in core, plugins or scripts (the `events` hits are Google Calendar API). | Remove (confirm with Brian). |
| `discord.auto_voice_channel_id`, `discord.auto_voice_text_channel_id`, `discord.auto_voice_user_ids` | root, ang, ops (`..._text_...`: ang only) | Read only on fork voice branches (`origin/dev/voice-summaries`, `dev/voice/discord-conversation`: `plugins/platforms/discord/adapter.py` `_auto_voice_channel_id`). Not in baseline, `archive/main-before-v0225-20261025`, or `backup/brian-20261029-before-force-push`. `profiles/ops/scripts/health/discord-inbound-watchdog.py:154,172` also reads `auto_voice_text_channel_id`. | Voice ticket: restore the feature or remove keys; ops watchdog is a live consumer of the text-channel key. |
| `discord.voice_fx.{busy,cancellation,join,model_switch,restart_join,session_resume}_ack_phrases`, `discord.voice_fx.session_resume_user_turn_threshold` | root, ops | From fork commits `ccb71b7e1d`/`d3fd5d76af`/`abf02370aa` ("feat(discord-voice): add command acknowledgement flows", 2026-07-17), on voice branches only. Upstream reads `voice_fx.ack_phrases`/`ack_enabled`/`ambient_*`/gains (`plugins/platforms/discord/adapter.py:3550`), not the per-event lists. | Voice ticket. |
| `discord.group_allow_from` | rook | Discord adapter uses `allow_from` for groups (`adapter.py:1186` comment); only `qqbot` reads `group_allow_from`. | Remove or switch to `allow_from`; confirm with Brian. |
| `plugins.entries.gateway-restart-tool.schedule_delay_seconds` | root, ops | Plugin reads `cooldown_seconds`, `enabled`; not this key. | Remove. |
| `plugins.entries.private-journal.memory_retention_enabled` | root | Plugin reads `memory_capture_from`, `memory_provider`, `bank_id` etc.; not this key. | Remove. |
| `tts.elevenlabs.stability`, `.similarity_boost`, `.style`, `.use_speaker_boost` | root, ang, ops | `tools/tts_tool*.py` reads `voice_id`, `model_id`, `speed`, `max_text_length`, `base_url` for ElevenLabs; no reader for these four. `similarity_boost` appears on the fork voice branches only. | Remove, or restore voice settings in voice ticket. |

Not dead (checked because tiers were ambiguous): `skills.config.agentmail.{sender_inbox,brian_address}` are declared and read by the `agentmail` skill (`skills-library/pools/assistant/productivity/agentmail/SKILL.md`); `plugins.entries.orgcontext.settings.external_prompts` via `ctx.get_config('external_prompts')`; `plugins.entries.private-journal.memory_capture_from` via `memory.py`; `plugins.entries.disk-cleanup.settings.exempt_path_contains` via `disk_cleanup.py`; `plugins.entries.newprompt.allow_gateway_injection` via `hermes_cli/plugins.py:632`.

## 7. Open questions for Brian

1. `skills.prompt_description_limit` (4 profiles): keep the fork's configurable budget or accept upstream's fixed 60? (Setting is read by baseline only.)
2. Does any home use Camofox or `browser.backend: camofox` (ops)? If so, v0.23.0 installs the `camofox` plugin unattended on first start/update (`left_core_migration.py`, honouring `security.allow_lazy_installs`); otherwise nothing installs.
3. Remove `cron.events.*`, `discord.group_allow_from`, `checkpoints.delete_orphans`, `auxiliary.web_extract.*`, `auxiliary.skills_hub.*`, `schedule_delay_seconds`, `memory_retention_enabled` now (pre-cutover), so the post-cutover dead-key list is empty apart from voice keys?
4. Voice keys (auto_voice_*, voice_fx per-event, ElevenLabs voice settings): restore in the voice rebuild or drop?
5. Retire or rewrite `browser-smoke.py` for `browser_exec`?
