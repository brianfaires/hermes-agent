# Fork Audit — changes since v2026.6.19 (`2bd1977d8`)

**Goal:** minimize fork size and lower future maintenance (rebase/merge) cost.
**Base:** tag `v2026.6.19` = `2bd1977d8` (2026-06-19). 34 commits, ~15.5k insertions, 139 files.

## Review lens (from `AGENTS.md`)
- **Prompt caching is sacred** — no mid-conversation toolset swaps / prompt rebuilds.
- **Core is a narrow waist** — capability belongs in plugins/skills/hooks/config, not core growth.
- Maintenance cost ≈ conflict surface on the next rebase ≈ **churn in shared core files**, not line count.
- The cheapest fork commit is one you delete by **upstreaming** it.

## Disposition codes
`DROP` remove · `UPSTREAM` PR out of fork · `RELOCATE` →plugin/hook/new-file · `CONFIG` replace w/ config · `SQUASH` fold into feature · `SPLIT` non-atomic · `KEEP` fork-inherent / done right

---

## Core hotspots (where rebases will bleed)
| File | # fork commits | Nature |
|---|---|---|
| `gateway/run.py` | 9 | voice orchestration (5), cron-profile pin, Codex guard, kanban wiring |
| `cron/scheduler.py` | 5 | LLM summary, calendar attach, delivery alerts, prompt_path, hooks |
| `plugins/platforms/discord/adapter.py` | ~12 | already a plugin file — cheap churn |

---

## Cross-cutting findings
- **F1 — Voice = 1 immature feature across 10 commits leaking into core.** Adds ~15 funcs to `run.py`/`base.py`, reaches into `agent/context_compressor.py`. Plugin-platform + middleware/observer-hook contract (`docs/middleware/`) exist for exactly this. 5 `fix()` iterations = it landed unstable. **Biggest single fork-cost item.**
- **F2 — Cron features bypass the hook API this fork built.** `aeee4ce23` added a clean `CREATE/UPDATE/REMOVE/COMPLETE` registry; later cron commits edit `scheduler.py` directly instead (`1236d5393`'s own docstring says "Brian-local hook").
- **F3 — Profiles are UPSTREAM first-class.** So profile-scoping `local:` commits are likely *upstream profile-isolation fixes*, not fork-inherent → candidates to contribute back. Verify per-commit.
- **F4 — Two commits expand core for little value.** `541906aa2` (defensive band-aid, +178 in `run.py`); `2e7ab7db9` (`debug()` left 437 always-on `logger.info` lines, mixed with real filter logic).
- **F5 — Non-atomic.** `774b8c368` ("branch metadata **and** telegram routing"); `2e7ab7db9` (debug + filter logic).
- **Positive calibration:** `54d1007b6` (Hindsight decouple via toolset config) and `496b07e36` (kanban mirror, 5555 lines almost entirely in new files, +15 in `run.py`) are the model of "done right."

---

## Per-commit disposition & progress
Status: ⬜ not started · 🔍 deep-diving · 📋 plan ready · ✅ done

| Commit | Disp. | Status | Why / criterion |
|---|---|---|---|
| `541906aa2` Codex/Claude repair | DROP | ⬜ | (1) defensive band-aid, +178 in `run.py` |
| `2e7ab7db9` debug filter logging | SPLIT+DROP | ⬜ | (1,4) 437 always-on info lines; keep filter logic |
| `1236d5393` cron→calendar | RELOCATE→hook | 📋 | (2) self-called "local hook"; use `COMPLETE` |
| `3bfc4b6ab` cron LLM summary | RELOCATE→hook | 📋 | (2) +137 in `scheduler.py` |
| `5c1106457` cron delivery alerts | RELOCATE→hook | 📋 | (2) +172 in `scheduler.py` |
| `417a154c8` voice chat | RELOCATE→plugin | 📋 | (3) 682 lines leaking into `run.py` |
| `5ed7c1216` ephemeral replies | SQUASH+RELOCATE | 📋 | (3,4) voice fix; core+18 locales |
| `526fa2035` TTS profile scope | UPSTREAM? | 📋 | (3) profile-isolation fix — verify |
| `ac8f47013` voice greeting | SQUASH+RELOCATE | 📋 | (3,4) +107 in `run.py` |
| `afc557d4b` speak prompts | SQUASH+RELOCATE | 📋 | (3) +175, 7 new `run.py` funcs |
| `12e264bf4` voice compression | SQUASH+RELOCATE | 📋 | (3) bleeds into `context_compressor.py` |
| `8dffad7d9` voice receive | SQUASH | 📋 | (4) mostly plugin-local |
| `9f2638e4e` abbreviate tool-call | RELOCATE | ⬜ | (3) Discord display in core `agent/display.py` |
| `774b8c368` kanban branch + tg routing | SPLIT | ⬜ | (4) two concepts |
| `412c37925` shared skills layer | CONFIG? | ⬜ | (2) core `prompt_builder`; external skills-dirs as config? |
| `32e61e925` escape markdown | UPSTREAM | ⬜ | general Discord bug |
| `04b06285c` escaped backticks | UPSTREAM | ⬜ | general Discord bug |
| `0d94dad3e` slash-sync retry | UPSTREAM | ⬜ | general robustness fix |
| `d9d6f2309` langfuse path payloads | UPSTREAM | ⬜ | general; plugin-local |
| `f55ded481` disk-cleanup wildcard | UPSTREAM | ⬜ | general; plugin-local |
| `95a6435bc` MEDIA safeguards | REVIEW | ⬜ | (1) core `delivery.py`/`base.py` — defensive? |
| `e8b08e77e` webhook OIDC triggers | KEEP/UPSTREAM | ⬜ | one file, coherent |
| `6f3ec4f57` tool-search visible | KEEP | ⬜ | cache-aware, config-driven |
| `79b268522` ElevenLabs settings | KEEP | ⬜ | config-driven, `tts_tool.py` only |
| `cb9c59841` STT aliases | KEEP | ⬜ | config + plugin-local |
| `17e47442f` auto-threading | KEEP | ⬜ | plugin-local |
| `7a5e9b1a1` Raft quiet | KEEP | ⬜ | 2 lines, plugin-local |
| `54d1007b6` Hindsight decouple | KEEP ✅model | ⬜ | toolset config done right |
| `496b07e36` kanban mirror | KEEP ✅model | ⬜ | new files; consider `gateway/`→`plugins/kanban/` |
| `aeee4ce23` cron hooks | KEEP | ⬜ | intended-pathway infra; enables RELOCATEs |
| `be09b9e42` cross-profile events | KEEP | ⬜ | new `event_bus.py` + uses hook API |
| `3d074de3a` absolute prompt_path | KEEP/UPSTREAM | ⬜ | coherent general capability |
| `b46b320b7` pin cron to profile | UPSTREAM? | 📋 | profile-isolation fix — verify |
| `2e1b71321` scope model switches | UPSTREAM? | 📋 | profile-isolation fix — verify |
| `87beba68b` reply-mode fixture | SQUASH | ⬜ | test-only; fold into kanban |

---

## Recommended sequence (by cost reduction)
1. **DROP** `541906aa2` + debug half of `2e7ab7db9` — instant core-surface cut, no feature loss.
2. **UPSTREAM** the general fixes (`32e61e925`, `04b06285c`, `0d94dad3e`, `d9d6f2309`, `f55ded481`) + verify profile-isolation fixes (`526fa2035`, `b46b320b7`, `2e1b71321`) as upstream candidates.
3. **RELOCATE** 3 cron features onto the `COMPLETE` hook API — de-cores `scheduler.py`.
4. **Consolidate** the 10-commit voice feature into a discord-platform plugin module via the middleware/hook contract. Biggest win.
5. **SPLIT** `774b8c368`; decide if `412c37925` can be config.

---

## Deep-dive plans (pass #2)

### DD-1 — Voice cluster → extract `GatewayVoiceMixin` (RELOCATE, low risk)
**Finding:** the Discord adapter (plugin) *already* owns low-level voice I/O correctly —
`play_tts`, `send_voice`, `join_voice_channel`, `leave_voice_channel`, `play_in_voice_channel`,
voice mixer, STT aliases, auto-voice presence. The leak is **~25 orchestration methods bolted onto
`GatewayRunner`** in core `run.py` (`_load_voice_modes`, `_handle_discord_auto_voice_join/leave`,
`_handle_voice_channel_input`, `_maybe_send_clarify_voice_prompt`, `_should_send_voice_reply`,
`_send_voice_reply`, `_enrich_message_with_transcription`, clarify/approval TTS formatters, …).

**Pathway already exists:** `GatewayRunner(GatewayAuthorizationMixin, GatewayKanbanWatchersMixin,
GatewaySlashCommandsMixin)` at `run.py:2464`. Mixins live in their own files
(`gateway/authz_mixin.py`, `gateway/kanban_watchers.py`, `gateway/slash_commands.py`).

**Plan:** create `gateway/voice_mixin.py` defining `GatewayVoiceMixin`, move the ~25 `self.`-bound
voice methods there verbatim, add it to the base-class tuple. Near-mechanical, moves ~1000 lines out
of the #1 hotspot with ~zero behavior change. Do this *before* touching the middleware contract —
the mixin split alone captures most of the maintenance win and is far lower-risk than a rewrite.
**Then** SQUASH the 5 voice `fix()` commits into the extracted module's history so the feature reads
as one unit. `12e264bf4` (voice bleed into `agent/context_compressor.py`) is the one piece that
can't move to the mixin — audit whether that hook into compression is truly necessary.

### DD-2 — Cron calendar attach → COMPLETE hook (RELOCATE, trivial)
**Finding:** `_attach_cron_output_to_calendar()` is called inline at `scheduler.py:2354`; the helper's
own docstring calls itself a "Brian-local hook." A real `cron_hooks.emit(COMPLETE, …)` already fires
at `scheduler.py:2442` — the intended pathway (`cron/hooks.py` docstring even names `cron_calendar_sync`).
**Plan:**
1. Add `output_file=output_file` to the COMPLETE emit payload (`scheduler.py:2442`) — backward-compatible
   per the hook contract ("new payload fields can be added without breaking existing hooks").
2. Move `_attach_cron_output_to_calendar` into a small `cron_calendar_sync` module that does
   `cron_hooks.register_hook(COMPLETE, attach_output_to_calendar)`.
3. Delete the inline call at `:2354` and the helper from `scheduler.py` (−~35 core lines).

**Caveat:** attach currently runs *before* delivery (`:2354`); COMPLETE fires *after* delivery/notify
(`:2442`). Independent of delivery, so the timing shift is safe — note it and verify.

### DD-3 — Cron `delivery_summary` (`3bfc4b6ab`) / delivery alerts (`5c1106457`)
More entangled than DD-2: summary logic sits in the delivery path (`scheduler.py` ~847–1004), not a
clean tail call. Partial relocation only — extract the summary *builder* into a helper module and keep
a thin call site, or gate more of it behind `cron.delivery_summary` config (already partly config-driven).
Lower priority than DD-1/DD-2.

**Resolved 2026-07-07:** `delivery_summary` removed from the fork entirely — notification brevity
belongs in the cron job prompts themselves. The delivery *alerts* half (attention-emoji prefixes,
thread warnings) survives as `local: refine cron delivery alerts and warnings`.

### DD-4 — Profile-isolation commits are likely UPSTREAM bugs, not fork-inherent
Profiles are first-class upstream. `526fa2035` (TTS not profile-scoped), `b46b320b7` (cron not pinned
to profile home), `2e1b71321` (model switches not profile-scoped) each read as **upstream profile-leakage
fixes**. Verify each is generic (no Brian-only policy) and PR upstream — that deletes them from the fork
entirely. `b46b320b7` (+208 in `run.py`) is the highest-value one to offload.
