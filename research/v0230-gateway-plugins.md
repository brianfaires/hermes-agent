# v0.23.0 research: gateway, plugin, Discord and media fork features

Ticket: brianfaires/hermes-shared#55 (map #52). Baseline `3d5a9416f2`, base `v0.22.5`, target `v0.23.0`.
Method: value gate, then placement gate (ladder: config, skill, profile plugin, in-tree plugin, generic hook, minimal core seam, core patch).

## Method and evidence rules

- Upstream coverage: `git show v0.23.0:<path>` and `git grep v0.23.0`. Old contracts were run against a pristine `v0.23.0` export using the fork's own tests plus small probes (scratch only; live checkout untouched).
- Consumer evidence: key names, paths and counts only. No values, IDs, secrets or message text. `.env`/`auth.json` not read.
- None of the nine commits has a `fixup!` owner. `c09cbd4956` is a follow-on of `826182134d` (same feature). `371bbeb7ad`, `b21ffda911` and `59edc1d4fe` mix behaviors; split below.
- Probe results are indicative: tests that import fork-only modules fail by import, which proves absence, not behavior.

## Summary

| # | Fork feature | Commits | v0.23.0 | Disposition | Placement |
|---|---|---|---|---|---|
| 1 | Restart recovery continues authorized work | `6e1716efee` | not covered | keep | core patch (one function, prompt text) |
| 2a | Plugin text-mode args keep raw text (all surfaces) | `371bbeb7ad` | not covered | keep | minimal core seam |
| 2b | Discord/Telegram retain original `/command` text | `371bbeb7ad` | not covered | keep (decide with 2a) | core patch in adapters |
| 2c | CLI/TUI paste kept for slash commands | `371bbeb7ad` | not covered | keep only if Brian uses `/log` there | core patch |
| 3a | Plugin prompt-section caps 4k/8k to 10k/20k | `06cfe5b6cb` | not covered | keep | minimal core seam (constants) |
| 3b | `skip_context_files` in section info | `06cfe5b6cb` | not covered | keep | minimal core seam (1 key) |
| 4 | systemd drop-in aware unit comparison | `c81943501e` | not covered | drop-low-value (ask) | none |
| 5 | Discord `_build_client` / `_connect_voice_channel` seams | `b21ffda911` | not covered | keep | minimal core seam (2 methods) |
| 6 | Reset follow-up prompts (`newprompt`) | `d75b51ef1d` | not covered | keep, split in two slices | generic hook kwargs + minimal core seam; authority proof as core patch |
| 7a | `fallback_activated` hook, main-agent/startup/CLI/TUI | `826182134d` | not covered | keep | generic hook (one emit helper) |
| 7b | Aux and compression fallback events | `c09cbd4956` | not covered | drop-low-value (ask) | none; use `post_auxiliary_call` |
| 8a | Standalone-only MEDIA directives (gateway delivery) | `59edc1d4fe` | not covered | keep | core patch (new module, thin call sites) |
| 8b | Same rule for API-server image rendering | `59edc1d4fe` | not covered | drop-low-value | none |
| 8c | `hermes send` guidance wording | `59edc1d4fe` | not covered | keep with 8a | core patch (2 lines) |

## 1. Restart recovery continues authorized work (`6e1716efee`)

- Does: rewrites `build_resume_recovery_note` (`gateway/run.py`) so auto-resume continues the first unfinished authorized step after checking durable receipts; a newer user message wins; interactive platforms no longer get "report restored, ask what next". Remaining hunks only reword comments (`gateway/platforms/base.py`, `api_server.py`, `run_turn_runner.py`).
- v0.23.0: `gateway/run.py:1040` `build_resume_recovery_note` is byte-identical to `v0.22.5` (interactive branch still says "ask what they would like to do next"; "skip any unfinished work"). Fork test `tests/gateway/test_restart_authorized_continuation.py`: 3 failed on v0.23.0.
- Consumers: `gateway-restart-tool` enabled in default, `ang`, `ops` configs (`plugins.enabled`, `plugins.entries.gateway-restart-tool.{enabled,cooldown_seconds,schedule_delay_seconds}`); `logs/gateway-restart-tool.jsonl` 69 lines (+ `profiles/ang/logs/`). Policy behavior, not an API consumer.
- Disposition: keep. Placement: core patch, only the `build_resume_recovery_note` body (no hook or config exists for the note). Candidate upstream PR.
- Rebuild: function untouched upstream, so the hunk should apply cleanly. Drop the comment-only hunks: `api_server.py` (-538 lines upstream) and `base.py` (-205) churn heavily for zero behavior.
- Question: still want restart never to ask "what next" on interactive chats (Discord/Telegram)? That is the actual behavior change.

## 2. Plugin text-mode commands preserve pasted text (`371bbeb7ad`)

- Does: `plugin_command_args(name, command, fallback)` (`hermes_cli/plugin_command_dispatch.py`) returns the text after one separator for commands registered `argument_mode="text"`. Call sites: gateway (`run_inbound_hooks.py`), `tui_gateway/methods_tools.py`, `gateway/session_commands.py`, `cli.py`. Then per-surface fixes: Discord keeps the original message text (`discord/command_text.py`, `strip=False` on auto slash proxy), Telegram skips mention-stripping for slash text (`group_trigger_text(..., event=)`), CLI/TUI stop `.strip()`ing slash input and expand collapsed pastes (`cli_modal_mixin.py`, `cli_tui_mixin.py`, `ui-tui/src/app/useSubmission.ts`).
- v0.23.0: `argument_mode` exists on `register_command` (`hermes_cli/plugins.py:651`) but is only descriptor metadata. Dispatch still strips: `gateway/run_inbound_hooks.py:284` `event.get_command_args().strip()`, `cli.py:1221`, `tui_gateway/methods_tools.py:1064-1066`, `cli_tui_mixin.py:1702`, `cli_modal_mixin.py:135`, Discord `strip=bool(args_hint)` (`adapter.py:4563`), `telegram_context.py:85-90`. Not covered.
- Consumers: `private-journal` plugin (`plugins/private-journal/__init__.py`: `ctx.register_command('log', ..., argument_mode='text')`), enabled only in root `config.yaml` (default profile); `plugins.entries.private-journal.*` set (`vault_path`, `memory_*` keys). Not enabled in `ang`, `ops`, `rook`. Without the seam `/log` still runs but loses whitespace/newlines.
- Disposition: keep, split into slices by behavior. 2a: `plugin_command_args` + three dispatch call sites, minimal core seam ("honor the `argument_mode` the command already declares"; good upstream PR). 2b: Discord/Telegram retention, core patch, two small hunks. 2c: CLI/TUI paste handling, three files plus a JS test, highest conflict (`cli_tui_mixin.py` +36 upstream, `cli.py` +31, `tui_gateway/methods_tools.py` +98, `adapter.py` +242, `telegram_context.py` +77 new file `telegram_resolved_prompt.py`).
- Rebuild: drop the unrelated Discord comment rewrite in the same hunk. Re-verify Telegram against the new resolved-prompt module.
- Question: is `/log` used from CLI/TUI paste, or only Discord/Telegram? If only messaging, 2c can drop (drop-low-value) and the slice shrinks by about half.

## 3. Plugin prompt sections: bigger caps and `skip_context_files` (`06cfe5b6cb`)

- Does: `DEFAULT/MAX_SYSTEM_PROMPT_SECTION_CHARS` 4,000 to 10,000, aggregate 8,000 to 20,000 (`hermes_cli/plugins_dispatch.py`); adds `skip_context_files` bool to `_plugin_session_info` (`agent/system_prompt.py`); docs.
- v0.23.0: constants unchanged (`plugins_dispatch.py:99-102`: 4_000/4_000/8_000); `plugins.py:974-975` raises `ValueError` when `max_chars` exceeds the cap; `_plugin_session_info` lacks the key (`system_prompt.py:86`). Fork test `test_plugin_prompt_sections.py`: 1 failed (`KeyError: 'skip_context_files'`), 1 passed.
- Consumers: `orgcontext` enabled in all four profile configs (`plugins.entries.orgcontext.settings.external_prompts` in default). Plugin hard-codes `MAX_CHARS = 10_000` and registers with `max_chars=MAX_CHARS`; on v0.23.0 that raises at registration, so org policy would silently be missing. Reads `info['skip_context_files']` (`__init__.py:31`). Root `ORG.md` is 4,313 bytes, already above the upstream 4,000 cap.
- Disposition: keep. Placement: minimal core seam (two constants plus one dict key). Candidate upstream PR (make the cap configurable).
- Rebuild: `plugins_dispatch.py` +84 upstream, but the constant block is untouched, so a small clean hunk.
- Latent fork bug to fix in the rebuild: `_PLUGIN_SECTION_FRAME_RE` in `agent/system_prompt.py` parses `section-chars:` with `[0-9]{1,4}`. A section of exactly 10,000 chars needs five digits, so restart/resume recovery fails the whole block. Use `{1,5}`. (From reading; not executed.)

## 4. systemd drop-in aware unit comparison (`c81943501e`)

- Does: `hermes_cli/gateway_unit_compare.py` (114 lines) merges operator drop-ins into both installed and generated unit settings before `systemd_unit_is_current` decides "outdated"; refuses uncertain comparisons.
- v0.23.0: `hermes_cli/gateway.py:3319-3333` still compares normalized text only. `refresh_systemd_unit_if_needed` (3350) rewrites the unit file and runs `daemon-reload`; drop-ins are separate files and are not touched. Fork test `test_systemd_dropin_comparison.py`: 7 failed, 4 passed (the 4 are native-behavior cases).
- Consumers: `~/.config/systemd/user/hermes-gateway.service.d/` holds `20-cron-timeout.conf` (`[Service] Environment`), `hardening.conf` (13 `[Service]` hardening keys), `hindsight.conf` (`[Unit] Wants`), plus `hermes-web-failover-ops.service.d/`. So drop-ins exist. Logs: 0 hits for "outdated"/"systemd unit" in `gateway.log*`/`agent.log`, i.e. no evidence of a repair clobbering them.
- Assessment: upstream's "repair" rewrites the base unit and leaves drop-ins in force, so the commit message's harm ("replaces valid operator overrides") is not shown on v0.23.0. The only gain is skipping a redundant unit rewrite and `daemon-reload` when live settings already match.
- Disposition: drop-low-value, pending Brian. Placement if kept: core patch (2 lines in `gateway.py` plus the new module); small conflict.
- Question: did a repair ever overwrite an override in practice? If yes, keep as-is.

## 5. Discord client/voice factory seams (`b21ffda911`)

- Does: `DiscordTransportMixin._build_client(**options)` replaces direct `commands.Bot(...)` in `connect()`; `_connect_voice_channel(channel)` replaces `channel.connect()`. Mixed: also moves `_resolve_channel` into the new `adapter_transport.py` (pure relocation).
- v0.23.0: `plugins/platforms/discord/adapter.py:1451` `commands.Bot(` and `:3762` `channel.connect()`; no hooks, no `adapter_transport.py`. Fork test `test_discord_client_factory.py` could not run (async plugin errored in the scratch harness); coverage judged by `git grep`.
- Consumers: `discord-outbound-policy` enabled in all four profiles; `FencedDiscord(base)` overrides both methods, registers via `ctx.register_platform('discord', adapter_factory=...)`, and raises `RuntimeError` at load if either seam is missing (fail-closed). On v0.23.0 without the seams Discord outbound policy and voice fencing would not load in any profile.
- Disposition: keep. Placement: minimal core seam. Do not carry the `_resolve_channel` move or the new mixin; add the two methods directly on `DiscordAdapter`, about 10 lines, much lower conflict than the fork shape (`adapter.py` +242 upstream).
- Open: could upstream accept the seam PR? Not needed for the plan.

## 6. Reset follow-up prompts for `newprompt` (`d75b51ef1d`)

- Does: (a) `on_session_reset` also receives `session_key`, `message_id`, `command_metadata`, `source` and runs under the routed profile scope; `pre_gateway_dispatch` runs in profile scope. (b) `ctx.inject_message(..., expected_session_id=)` pins a gateway injection to the new session (`plugins.py`, `plugins_injection.py`, `run_plugin_injection.py`). (c) `gateway/session_reset_followup.py` (90 lines) carries private connector proof from the confirmed `/new` so an injection into the still-empty session is admitted without weakening ordinary injection (hook in `session_automation.snapshot_automation`).
- v0.23.0: `gateway/slash_commands_session.py:258` still calls `on_session_reset` without those kwargs; no `expected_session_id` on injection (only unrelated CAS in `session.py:1293`); no reset-follow-up proof. Not covered.
- Consumers: `newprompt` enabled in all four profiles with `plugins.entries.newprompt.allow_gateway_injection`; its `reset()` reads `command_metadata`, `new_session_id` and calls `inject_message(..., expected_session_id=...)`; Discord `/new prompt:` via `register_platform_handler`. Logs: 174 mentions of "newprompt", 0 "reset follow-up refused".
- Disposition: keep, as two slices. Slice A (generic hook kwargs plus `expected_session_id`, about 40 lines, minimal core seam). Slice B (`session_reset_followup.py` plus `snapshot_automation` hook, core patch) because an authority-backed gateway refuses ordinary plugin injection into empty sessions.
- Rebuild risk: highest of the set. `gateway/session_automation.py` +253 upstream and `v0.23.0` commits touch the same authority path, notably `439e874e2f` (owner lock held across `/new`), `91171b7f0b`, `341afcbf9a`. Re-derive slice B against the new owner-lock and committed-route model; do not replay the hunk. Slice A should apply with offsets.
- Question: is `/new <prompt>` worth the authority patch? Fallback if not: slice A only, and newprompt degrades to a no-op for fresh sessions.

## 7. `fallback_activated` hook (`826182134d`, `c09cbd4956`)

- Does: new hook (`VALID_HOOKS` in `hermes_cli/plugins.py`) emitted by `agent/fallback_events.py::emit_fallback_activated` from startup (`agent_init_fallback.try_init_fallback_client`, which also moves about 95 lines out of `agent_init.py`), turn (`chat_completion_helpers`), CLI (`cli_agent_setup_mixin`), TUI (`tui_gateway/agent_factory.py`) and shared runtime resolution (`runtime_provider.py`). Payload is bounded (no raw error). `c09cbd4956` adds `auxiliary_fallback_events.py` for auxiliary and compression fallbacks, `event_id`, `task`, `profile_name` override; it refactors `auxiliary_client.py` and `context_compressor.py`.
- v0.23.0: no `fallback_activated` anywhere (`git grep`; probe `"fallback_activated" in VALID_HOOKS` is False). Upstream only logs: `chat_completion_helpers.py:1897` `_log_fallback_activated` (INFO, billing WARNING). `pre/post_auxiliary_call` hooks already exist upstream with `aux_task`, provider and model.
- Consumers: `hermes-extensions/commands/fallback-alert/notify_fallback_alert.py` (commit `a3c8d1c`, #12) is written against `fallback_activated` (event IDs, receipts). It is not registered yet: no profile `hooks:` key lists `fallback_activated`; the live hook is the legacy `post_api_request` script (`hooks.post_api_request` in root, `ang`, `ops` configs; `shell-hooks-allowlist.json` has that one entry). The README says to replace the legacy script and not run both. Fallbacks do happen: 14 "Fallback activated" lines in `logs/agent.log` (2026-10-31 to 2026-11-06).
- Disposition 7a: keep. Placement: generic hook (new observer name plus one emit helper). Keep emits at the turn (`try_activate_fallback`) and startup/runtime stages; they are one-line calls at existing log sites. Move the 95-line init refactor only if the init stage is wanted; otherwise emit from the existing init-fallback site without moving code (`agent_init.py` +243 upstream).
- Disposition 7b: drop-low-value, pending Brian. Aux and compression fallback is high-volume, needs the `auxiliary_client.py` (+371) and `context_compressor.py` (+155) refactors, and `post_auxiliary_call` already reports the provider actually used, so the alert script can compare to configured provider without core changes.
- Lower-divergence alternative to all of #7: let the alert consume `post_api_request` plus `post_auxiliary_call` and diff provider/model against config (this is how the legacy script works). Cost: per-request invocation, no startup/CLI stage, no billing reason.
- Question: should the alert cover auxiliary/compression fallback, or only the main model?

## 8. Standalone-only MEDIA directives (`59edc1d4fe`)

- Does: `gateway/media_directives.py` (115 lines): a `MEDIA:` tag delivers only when it is a complete standalone line (outside prose, inline code and fences; paths with spaces validated against disk); one eligibility rule for extraction, display stripping, auto-append and API image rendering; historical token grammar kept only for dedup hints. Call sites: `gateway/platforms/base.py`, `gateway/run.py`, `run_turn_runner.py`, `stream_consumer.py`; API server split into `api_server_media.py`; `prompt_builder.py` and `hermes_cli/send_cmd.py` wording.
- v0.23.0: `base.py:1220` `MEDIA_TAG_CLEANUP_RE` still matches tags inline. Probe on `BasePlatformAdapter.extract_media`: a prose sentence containing `MEDIA:<existing path with space>` yields 1 attachment; a backtick-wrapped example yields 1; a fenced block yields 0; a standalone line yields 1. So literal examples in prose and inline code still deliver files on v0.23.0. Fork tests importing `gateway.platforms.api_server_media` fail by import.
- Consumers: no api_server platform in any config (`platforms` keys: webhook/discord/telegram; 0 log hits for `/v1/...`). Cron `daily-deliver-morning-brief` (default profile `cron/jobs.json`) emits `[[audio_as_voice]] MEDIA:...`; that stays standalone because control tokens are masked in the fork scan. Skills: 13 files in `skills-library` and 3 in `skills` mention `MEDIA:` (for example `gateway-troubleshooting/references/media-directives.md`, `cron-operations/references/cron-media-directive-delivery.md`, `dogfood/SKILL.md` which tells agents to write `MEDIA:<screenshot_path>` mid-sentence). These docs and agents themselves quote directives, which is exactly the risk the fork rule removes. The `hermes send` caption form changes (caption on the preceding line).
- Disposition: 8a keep (core patch: new module plus thin call sites; no seam exists). 8b drop-low-value (no API-server consumer; `api_server.py` was cut by 538 lines upstream, so this is the worst conflict). 8c keep with 8a. Candidate upstream PR.
- Rebuild: `base.py` +205 upstream, so re-apply the call-site edits by hand around `_mask_*`/`_extract_*` helpers; the new module itself is conflict-free. Re-run the fork's contract tests (`test_extract_local_files`, `test_media_producer_control_eligibility`, `test_media_resend_dedup`) as the acceptance check. Update the media skills if the dogfood wording needs a standalone line.
- Question: any workflow that relies on `caption MEDIA:/path` on one line (Telegram sends, cron prompts)? The one cron match is control-token form, which stays valid.

## Open questions for Brian

1. #1: keep "never ask what next after restart" on interactive chats?
2. #2: is `/log` used from CLI/TUI paste, or only Discord/Telegram?
3. #4: did a unit repair ever clobber an override? If not, drop.
4. #6: is `/new <prompt>` worth the authority patch, or take slice A only?
5. #7: aux/compression fallback alerts wanted, or main model only? Register `fallback-alert` in place of the legacy script as part of cutover?
6. #8: OK to drop API-server image rendering (no consumer)?
7. Upstream PRs worth carrying (not blocking): argument_mode text (#2a), prompt-section caps (#3), Discord seams (#5), standalone MEDIA (#8a).

## Expected rebuild outcome

Clean or near-clean: #1, #3, #7 emit helper. Moderate: #2a/2b, #5, #8a (call sites). Hard: #2c, #6 slice B, #8b. Net simplification if drops are accepted: #4, #7b, #8b, #2c, and the `_resolve_channel` move.
