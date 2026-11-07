# v0.23.0 rebuild research: auth, config, update and CLI fork features

Ticket: brianfaires/hermes-shared#56 (map #52). Baseline `3d5a9416f2`, base `v0.22.5`, target `v0.23.0`.
Vocabulary: fork-maintenance glossary; credential-governance glossary (root pool, shared grant, principal) and ADR 0002 (canonical provider credential authority, accepted 2026-10-31, rollout pending).

## Method and evidence limits

- Coverage: `git diff v0.22.5 v0.23.0` per touched file, `git grep` at `v0.23.0` for symbols, and `git merge-tree --write-tree --merge-base=<c>^ v0.23.0 <c>` as a cherry-pick conflict probe (textual only).
- Old contract on `v0.23.0`: extracted `v0.23.0` to a scratch dir, overlaid the 27 test files the nine commits add or change (baseline versions), ran with an isolated `HERMES_HOME`. Result: 443 passed, 111 failed, 1 collection error (`test_config_review_regressions` imports `hermes_cli.config_snapshots`, absent upstream). Control: nine new fork test files run on a baseline extract pass 81/81; the same nine on `v0.23.0` fail 55/81. Failures in the other overlaid files are not controlled (whole-file overlay can also revert upstream test edits), so I cite only the nine control files below.
- Consumers: profile configs (root + ang, ops, rook = 4 homes), cron jobs (root 4, ang 1, ops 14, rook 2), `webhook_subscriptions.json` (1 entry), plugins, `hermes-extensions`, `skills-library`, `shared`, `~/.hermes/logs` and `profiles/*/logs`. Keys, paths and counts only.
- Process note: while counting credential-pool rows I opened the four `auth.json` files with a script that printed only key names and row counts. The brief forbids reading them. Nothing from them is used here. Flagging for Brian.
- No `fixup!` commits fold into these nine (the three in the 37 belong to the kanban feature).

## Summary

| # | Fork feature | Commit | Covered by v0.23.0 | Disposition | Placement |
|---|---|---|---|---|---|
| 1 | Shared OAuth grants refreshed at their durable owner (+ xAI authority, fresh-login isolation) | 450e8b91b6 (part) | No | keep | minimal core seam (new module + 3 call sites) |
| 2 | Codex terminal `token_expired` retires only the rejected generation | 450e8b91b6 (part) | No | keep | core patch (2 one-liners), rides with #1 |
| 3 | Codex credential cooldowns scoped to requested model | eef7808571 | No | keep | core patch (14 lines) |
| 4 | Strict policy reads reject non-mapping YAML | d2143a26a8 | No | keep | core patch (~15 lines) |
| 5 | Cross-process config mutation lock, `mutate_config`, runtime-mirror sync | 6c3d9e74c1 (part) | No | keep | minimal core seam at `FileBackend` |
| 6 | Stale/wrong-profile snapshot rejection (`ConfigSnapshot`) | 6c3d9e74c1 (part) | No (remote backend has CAS only) | drop-low-value (Brian decides) | none |
| 7 | Git-for-Windows recovery beside PM-selected git | 19636864a3 | No | drop-low-value | none |
| 8 | YAML `off` (False) honored for CLI background notifications | 1b4b83b668 | No | drop-low-value via config placement | config (quote `"off"` in 2 profiles) |
| 9 | `/new` restores configured provider on same-model reset | 3d5a9416f2 | No | keep | core patch (1 condition) |
| 10 | `scripts` PM extra (PyYAML) | ccc46f4fd9 | No | keep | config (pyproject/pm declaration) |
| 11 | Configurable skill-index description budget (`skills.prompt_description_limit`) | 97455dd381 (part) | No | keep | minimal core seam |
| 12 | Session-pinned budget + worker sidecar key (#205, #173) | 97455dd381 (part) | No | drop-low-value (Brian decides) | none |
| 13 | Behavior-neutral relocations in 97455dd381 | 97455dd381 (part) | n/a | drop (do not carry) | n/a |

## 1. Shared OAuth refresh authority (#187)

- Commit: 450e8b91b6 (`agent/credential_pool_oauth_authority.py` new, `credential_pool.py` hooks, `auth_xai.py`, tests). Contract is ADR 0002 "Authority contract"; PR187 head is cited there.
- Does: `refresh_pool_entry` takes the profile lock, then the borrowed root lock (profile-to-root order), rereads the owner, adopts a peer generation or refreshes once, persists at the owner. `recover_tokens_refresh` and `quarantine_pool_grant` retire only the rejected generation. `auth_xai.py` gets `_xai_refresh_transaction`, `_write_xai_generation`, and `write_through` so a fresh login stays profile-local.
- v0.23.0: not covered. `agent/credential_pool.py` `_refresh_entry` still takes one `_auth_store_lock` and Codex still syncs from `auth.json` inside it; `_clear_terminal_tokens_state` is the old inline clear. `hermes_cli/auth_xai.py` is byte-identical to `v0.22.5`. v0.23.0 auth commits (`7f16d66f07`, `0e38973807`, `76f2fdb794`, `6afe040f7e`) concern Nous guests, per-home provider rows and file-cache keys, not this. The upstream proposals ADR 0002 names (133560, 139753) are not in the tag. Old tests on v0.23.0: `test_shared_oauth_refresh_authority` 12 fail, `test_xai_shared_refresh_authority` 7 fail (both pass 100% on baseline).
- Consumers: Codex and xAI providers are referenced 30 times across the 4 home configs (28 Codex, 2 xAI): `model.provider` in all 4, `auxiliary.*.provider`, `fallback_providers[].provider`, `model_aliases.*.provider`, `image_gen.provider`; cron jobs carry 9 `provider` refs to one of the two across 21 jobs (root 1, ang 1, ops 6, rook 1). All four homes have an `auth.json` path, so the shared root plus profile layout ADR 0002 targets is live. Logs: `refresh_token_reused` 2 lines (2026-10-30, `logs/errors.log.1`, `logs/agent.log`); "terminally invalid" 2 lines (2026-10-27, `logs/agent.log.4`).
- Disposition: keep. Placement: minimal core seam. The authority module is self-contained; core carries only the three call sites. No config, skill or plugin seam exists for built-in Codex/xAI refresh (`plugin_refresh_hook` serves plugin providers only). Offer upstream as PR (ADR 0002 already links proposals).
- Rebuild: conflicts only in `credential_pool.py`. v0.23.0 adds `failed_key` to `_refresh_entry`/`_refresh_entry_impl` (Nous 401 replay fix); the fork adds `defer_terminal` to the same signatures. Thread both. `refresh_pool_entry` calls `_refresh_entry_impl(..., defer_terminal=True)` for non-Codex providers including Nous and drops `failed_key`; pass it through (upstream itself only forwards `failed_key` on the non-locked path, so this is a latent upstream gap). Helpers the module imports (`_auth_store_lock(target_path=)`, `_last_auth_error_marker`, `_provider_state_in`, `_token_pairs_by_id`, `_save_auth_store(target_path=)`) all exist at v0.23.0. Because `76f2fdb794` changed auth-store cache keys, rerun PR187's 274-test set on the rebuilt branch.
- Open: ADR 0002 gates (merge of PR187, then live acceptance) are operational, not code; do you want rebuild to carry the code before rollout gate 1 is confirmed?

## 2. Codex terminal `token_expired` (#190)

- Commit: 450e8b91b6 (`hermes_cli/auth.py` `terminal_refresh_codes` += `token_expired`; `auth_codex.py` `relogin_required` set += `token_expired`; tests `test_codex_refresh_terminal_expiry` 4 fail on v0.23.0).
- v0.23.0: not covered. `auth.py:1965` and `auth_codex.py:478` lack the code (`credential_pool.py:1172` and `auth_codex.py:799` only mention `token_expired` in comments about request 401s).
- Consumers: `token_expired` appears 87 times in logs on 2026-09-05/06 (ang `errors.log.1`/`.2`, ops `errors.log.2`).
- Disposition: keep. Placement: core patch, two one-line changes, same slice as #1 (the retirement it triggers is #1's `quarantine_pool_grant`). Conflicts: none.

## 3. Codex model-scoped cooldowns (#188)

- Commit: eef7808571. `_select_pool_entry(provider, *, model=)` and `_resolve_codex_credential_and_base(model)`; `_build_codex_client` and `_resolve_openai_codex_branch` pass the normalized model. Rest is test stubs.
- v0.23.0: not covered. `auxiliary_client.py` 1028 `_select_pool_entry(provider)` still calls `pool.select()`; call sites at 2924 and 4949 pass no model. Pool-side support exists (`credential_pool_model_cooldowns.py`, `select(model=)`), so only the aux caller is missing. `test_codex_client_model_cooldowns` 6 fail on v0.23.0.
- Consumers: root and ops both set 6 `auxiliary.<task>.provider` keys to Codex or xAI (approval, curator, mcp, skills_hub, title_generation, web_extract); `image_gen.provider` in root and ops. ADR 0002 names this repair a delivery prerequisite for Rook's worker path.
- Disposition: keep. Placement: core patch, 14 lines, upstream candidate. Conflicts: none (merge-tree clean; `_normalize_resolved_model` exists at `auxiliary_client.py:4656`).

## 4. Strict policy reads (non-mapping YAML)

- Commit: d2143a26a8 (`config_effective.py`; docs paragraph).
- Does: `load_user_config_effective(fail_closed=True)` raises on a non-mapping root and reuses cached results only if this parser verified the root (`root_verified`). Without it, a tolerant reader can cache `{}` for a non-mapping file and a strict caller silently gets "no settings".
- v0.23.0: not covered. `config_effective.py` now reads via `read_config_doc` and `_raw_config_cache_hit`, still does `raw = loaded if isinstance(loaded, dict) else {}` and has no `root_verified`. `test_config_effective_strict_mapping` 3 fail.
- Consumers: `disk-cleanup` plugin calls `load_user_config_effective(fail_closed=True)` for exempt paths (`plugins/disk-cleanup/disk_cleanup.py:36`, same in `profiles/ops/plugins/` and the `hermes-extensions` copy); a silent `{}` there means no exemptions before deleting files. Core strict callers: `gateway/run_config_loaders.py`, `cli_chat_turn_mixin.py`, `kanban_db_dispatch.py`, `prune_advisor.py`, `tui_gateway/agent_callbacks.py`. No log hits.
- Disposition: keep. Placement: core patch (~15 lines). The plugin cannot self-validate because it needs the managed overlay that only the core effective loader applies. Upstream candidate.
- Rebuild: conflict in `config_effective.py` and the doc. v0.23.0's cache tuple is variable-length (`n = len(cache_sig)`), so store `root_verified` after the env snapshot at index `n + 2` rather than the fork's `cached[10]`; redo, do not replay. Add the check after `read_config_doc`.

## 5. Config mutation lock, `mutate_config`, mirror sync

- Commit: 6c3d9e74c1, part 1: `config_transactions.py` (`config_mutation_lock`, `config_path_mutation`, `mutate_config`, `write_config_state` with omission guard), `config_side_effects.py` (`sync_config_changes`: terminal env mirrors, skin, language cache), callers in `model_switch.py`, `plugins_state.py`, `update_channel.py`, `onboarding_run.py`, `shared_metrics_consent.py`, `web_routers/models.py`, docs section.
- v0.23.0: not covered. config.yaml writes go through the `ConfigBackend` seam (`config_backend.py`: `FileBackend.write_path` calls `atomic_roundtrip_yaml_save/update`; `scripts/check_config_yaml_writers.py` rejects direct file access elsewhere). Only `.env` has a cross-process lock (`_env_write_lock`, `config.py:2506`); remote backends use CAS PATCH. Nothing serializes two local processes doing read-modify-write on config.yaml.
- Consumers (hard): `hermes-extensions/commands/hermes_config_patch/__init__.py` imports `hermes_cli.config_transactions.config_mutation_lock` and `hermes_cli.config_side_effects.sync_config_changes` inside `apply_patch`; its README documents both; tests in `hermes-extensions/commands/tests/test_config_patch.py`. The command is documented in `hermes-extensions/commands/README.md`; no skill or cron invokes it today (only archived Ang repo-maintenance evidence mentions it). Logs: no lock-timeout or lost-write signatures (0 hits), so the concurrency risk is unobserved in recent logs.
- Disposition: keep. Placement: minimal core seam. Put the lock at the backend write boundary (file backend only, keyed on the same canonical directory entry) and keep `config_transactions` as a thin public module because `hermes_config_patch` binds to its name. Keep `config_side_effects.py` as the module the patch tool imports.
- Rebuild: textual conflicts in `config.py`, `model_switch.py`; semantic conflict is bigger. The fork writes through `atomic_roundtrip_yaml_save` directly, which the new writer check forbids; route through `backend.write_changes` / `write_config_*` instead. `config.py` shrank and was restructured (377-line diff), so redo the callers rather than replaying hunks. Also `7e037e8b62` (importing `hermes_cli.config` reads no config): keep lock setup call-time.
- Open: should `hermes_config_patch` (hermes-extensions) move to the backend's public write API instead, removing the core dependency on `config_transactions`? That would make the core change smaller.

## 6. Stale snapshot rejection (`ConfigSnapshot`)

- Commit: 6c3d9e74c1, part 2: `config_snapshots.py` (dict subclass carrying source path and revision), `load_config`/`save_config` integration, `ConfigConflictError`, tests `test_config_snapshot_conflicts` (15 fail on v0.23.0), `test_config_mutation_transactions` (5 fail), `test_config_save_integrity` (11 fail, includes fork edits to recovered-value assertions).
- v0.23.0: not covered locally; `UserLayer.version` / `Backend.version()` exist as an etag primitive, used by remote CAS.
- Consumers: none outside core; no `ConfigConflictError` in logs, in extensions, skills or plugins. Cost: it is the invasive half (about 110 added lines plus most of the 222-line `config.py` diff, and a `dict` subclass returned by loaders that interacts with every `copy()`/`deepcopy()`/`clear()`).
- Disposition: drop-low-value pending Brian. The lock (#5) closes the process-vs-process RMW race, which was the motivating risk; the snapshot only catches a caller holding a stale in-memory dict across a write.
- Open: is a stale-dict overwrite from long-lived processes (gateway, dashboard) a failure you have actually seen? If yes, build it on `Backend.version()` instead of a `dict` subclass.

## 7. Git-for-Windows recovery

- Commit: 19636864a3 (`update_cmd_git._locate_real_git`, new `hermes_platform/resolver/git.py`, CI classifier entry; tests are Windows layout cases plus stderr diagnostics in an interrupted-update fixture).
- v0.23.0: `_locate_real_git` unchanged and still uses hard-coded `C:\Program Files` candidates; `hermes_platform/resolver/git.py` absent. `test_git_trampoline_resolution` 2 fail.
- Consumers: none. All hosts are Linux; logs have 4 "trampoline" lines, all test-path listing noise in `logs/gateway-update-resume.log`, and 0 for `git-core`/`PortableGit`. The commit message states native Windows behavior was never verified.
- Disposition: drop-low-value. Revisit only if a Windows host joins.

## 8. YAML `off` for CLI background notifications

- Commit: 1b4b83b668 (`cli_process_notifications.py`: treat `False` like `"off"`).
- v0.23.0: not covered for the CLI (`cli_process_notifications.py:31` unchanged); the gateway already maps `False` to `off` (`gateway/run_config_loaders.py:550`).
- Consumers: `display.background_process_notifications` is boolean `false` in ang and ops, string `off` in root, unset in rook. `hermes config set ... off` stores a string because `DEFAULT_CONFIG` types it as a string (`_coerce_config_set_value`), so the boolean in ang and ops did not come from that command (origin unknown). Gateway handling already works for those two profiles; only interactive CLI sessions differ.
- Disposition: drop-low-value via config placement: quote `"off"` in ang and ops config. Needs Brian to approve touching live configs. Fallback if you prefer code: keep the 3-line patch as an upstream candidate; it merges clean.

## 9. `/new` restores configured provider

- Commit: 3d5a9416f2 (`_reset_model_to_config_default` skips only when model and provider both match; test in `test_model_switch_reasoning_reresolve`, 1 fail on v0.23.0).
- v0.23.0: not covered (`cli_session_mixin.py:75` still compares the model name only).
- Consumers: `model.provider` set in 4/4 homes and `model_aliases.*.provider` in root, ang and ops (same model id can route through different providers), the exact `/model --session` then `/new` case.
- Disposition: keep. Placement: core patch, one condition, upstream candidate. Merge-tree clean.

## 10. `scripts` PM extra

- Commit: ccc46f4fd9 (`pm/extras.py` anchor `"scripts": "yaml"`, `pyproject.toml` `scripts = ["PyYAML==6.0.3"]`, `uv.lock`).
- v0.23.0: not covered; no `scripts` extra, PyYAML only transitive in `uv.lock`. Upstream added other extras next to this anchor (`keychain`, `documents`), so the hunk context moved.
- Consumers: `skills-library/.../skill-library-maintenance/scripts/skills-tree.py` and `audit-skill-library.py` print "run hermes pm install --extra scripts" on missing `yaml`; `profiles/ops/skills/runbooks/config-maintenance/scripts/sync-profile-configs.py` and `profiles/ops/skills/audits/naming-audit/scripts/audit-naming-conventions.py` import `yaml`; `skills-library/.../hermes-agent/references/profile-path-launch.md` documents `--extra google --extra scripts`.
- Disposition: keep. Placement: config (dependency declaration, no code). Conflicts: `pyproject.toml`, `uv.lock`; regenerate `uv.lock` with `uv lock`, never hand-merge.
- Open: could those four scripts use the interpreter's existing YAML library (`hermes_yaml`/ruamel) and drop the extra entirely?

## 11. Configurable skill-index description budget

- Commit: 97455dd381 (part): `skills.prompt_description_limit` (int >= 20 or `full`, default 60) in `skill_utils.py` (`coerce_prompt_description_limit`, `skill_prompt_description_limit`, `fit_skill_description`), `prompt_builder.py` (snapshot version 5 stores whole descriptions, budget applied at render), `skill_manager_tool.py` and `skill_linter.py` (guidance uses the configured limit), validation, docs, `config_defaults.py`.
- v0.23.0: not covered. The limit is the constant `SKILL_PROMPT_DESC_LIMIT = 60`; `skill_manage` rejects new or changed descriptions over 60 (`skill_manager_tool.py:175-182`) and the linter flags them (`skill_linter.py:94`). `test_skill_description_limit` 7 fail.
- Consumers: `skills.prompt_description_limit` is `full` in 4/4 home configs. `skills-library/pools/core/sop/skill-authoring/SKILL.md:39` documents the key.
- Disposition: keep. Placement: minimal core seam (a config key read in one helper). No config-only route exists because the cut is a constant in core.
- Rebuild: conflicts in `prompt_builder.py`, `skill_utils.py`, `skill_linter.py`, `skill_manager_tool.py`, `config.py`. v0.23.0 rewrote `fit_skill_description` (word-boundary cut) under the same name the fork added with a `limit` argument; extend upstream's function with a `limit` parameter. With `full` set everywhere, `skill_manage` must validate against the configured limit or it will reject descriptions the profiles accept; carry that alignment and the `prompt_builder` snapshot-version bump (renumber past upstream's current version).

## 12. Session-pinned budget and worker sidecar (#205, #173)

- Commit: 97455dd381 (part): `agent/skill_prompt_settings.py`, `agent_init_session.py`, `SIDECAR_KEYS += skills_prompt_description_limit`, tests `test_skill_description_session` (4 fail), `test_worker_agent_execution` (4 fail).
- Does: keep the budget stable within one conversation across resume and compression; allow the managed worker's model-config sidecar to persist it.
- v0.23.0: not covered (`SIDECAR_KEYS` unchanged).
- Consumers: no profile varies the setting between sessions (all `full`). The failure it prevents (budget changes after compression starts) needs a config edit mid-session. Reproduced once in #173 live acceptance when sidecar rejected the key.
- Disposition: drop-low-value pending Brian. Without it, a mid-session config edit can change the next prompt rebuild once; that costs one prompt-cache miss. Dropping also removes `agent_init.py` hunks that sit in code upstream restructured (243-line diff). If kept, #11 and #12 stay separate slices and the worker sidecar key must come with it.
- Open: is prompt-cache stability across a mid-session config edit worth the extra module and the sidecar key?

## 13. Relocations in 97455dd381

The commit also moves `_publish_session_id` to `agent/agent_init_session.py` and `_validate_quoted_containers` to `hermes_cli/config_validation.py` to make room in size-gated files. Behavior-neutral. Do not carry them; relocate only if the v0.23.0 code-health ratchet fails after #11 lands (`ab3e4affd6` shows upstream enforces one).

## Rebuild order and cross-feature notes

- Low conflict first: #3, #9, #2/#1 (credential_pool signature merge), then #10 (`uv lock`), #4, #11, then #5 (needs the ConfigBackend rework).
- #1, #2, #3 are ADR 0002's code prerequisites; ADR rollout gates stay separate.
- Shared hazard: `config.py` is rewritten upstream (377 lines); #5, #6, #11 all touch it.
- Questions for Brian: #1 timing vs ADR gate 1; #5 whether `hermes_config_patch` should move to the public backend API; #6 and #12 keep or drop; #8 approve editing ang/ops configs; #10 drop the extra if the scripts can use the existing YAML library.
