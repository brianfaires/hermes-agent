# Discord Voice Repair Handoff

Status as of September 10, 2026: Brian parked the Discord voice repair for higher-priority work. This is a source-level handoff only. It is not a release order, not a staging activation order, and not a resumption of task `engineering/t_6b5b4eda`, which remains blocked with no worker.

## Current Repository State

- Branch checked out in this workspace: `dev/discord-voice`.
- Candidate preserved locally and on origin: `fe99ce625b1ca46e60966fac857d516c1f35e430`.
- Candidate source base: `31bb2aed2f65d8d4193b97d57a11770009fba656`.
- Current `main`, `origin/main`, `staging`, and `origin/staging`: `cf62291dad2cd9c12de80bc5194ff76ef0f6ce55`, the Kanban worker prompt hotfix.
- `main` and `staging` contain no Discord voice restoration. No voice activation occurred.
- Historical workspace artifacts `STAGING-HANDOFF.md`, `ACCEPTANCE.md`, and `REVIEW-FIX.md` describe the old staging state and evidence. Their old release/staging language is superseded by this document. Their optional local root is `/home/brian/.hermes/kanban/boards/engineering/workspaces/t_6b5b4eda/`; they are outside Git and may disappear with workspace cleanup.

Do not treat the old staging handoff as a current release order. Do not treat the old base as a valid future rollback without rechecking the then-current repository, runtime, data, and dependency state.

## Intent and Scope

The parked patch restores the Discord voice MVP source path and adds sparse spoken progress around long-running work. It is relevant to FC-24 and the voice portions of FC-17, but it does not close blanket parity. ElevenLabs FC-04 and compression FC-25 remain outside this patch.

Canonical design and audit references outside the repository:

- `/home/brian/Documents/Projects/Plans/2026-09-09-conversational-discord-voice.md`
- `/home/brian/Documents/Projects/Reports/2026-09-10-v0.21-feature-migration-audit.md`

Brian's priority order was basic voice conversation, prior spoken slash commands, mid-turn TTS, then only low-risk latency/interruption improvements. Keep an independently usable baseline; no front agent or replacement orchestration framework.

## Implemented and Wired

The candidate implements these source behaviors:

- Configured Discord voice autojoin, startup presence sync, reconnect handling, and manual leave/off suppression.
- Profile-owned voice callbacks and voice mode state.
- Authorized receive path from Discord STT into the normal gateway message flow.
- Spoken alias rewriting with dynamic tail arguments, then dispatch through the normal slash-command handlers instead of a voice-only shortcut.
- Sparse lifecycle/tool progress speech and interim assistant commentary speech for voice sessions.
- Cancellation and stale-audio suppression so old turns do not keep speaking after invalidation, disconnect, or close.
- Preservation of explicit `/voice off` across automatic departure and return.

The alias catalog is profile-local `voice/commands.toml`, resolved via `get_hermes_home()` and loaded at adapter startup. Missing/malformed/invalid-UTF-8 catalogs fail soft. Unknown speech stays conversation; recognized aliases preserve argument tails and use normal command authorization. Configuration, authorized speakers/channels, transcript policy, and provider settings must be verified separately at any future activation; this handoff does not authorize changing them.

Primary source anchors:

- Autojoin/autoleave and voice progress gate: [`gateway/run.py`](../../gateway/run.py)
- Voice progress speaker and interim callback wiring: [`gateway/run.py`](../../gateway/run.py)
- `/voice off`, `/voice leave`, and voice mode command behavior: [`gateway/slash_commands.py`](../../gateway/slash_commands.py)
- Per-turn voice progress fields: [`gateway/turn_context.py`](../../gateway/turn_context.py)
- Discord STT alias rewrite, startup sync, and voice-state event hooks: [`plugins/platforms/discord/adapter.py`](../../plugins/platforms/discord/adapter.py)
- Voice mode tooling: [`tools/voice_mode.py`](../../tools/voice_mode.py)

Test anchors:

- Voice lifecycle, alias dispatch, cancellation, mute, and restoration regressions: [`tests/gateway/test_discord_voice_restoration.py`](../../tests/gateway/test_discord_voice_restoration.py)
- Sparse progress and interim commentary behavior: [`tests/gateway/test_run_progress_topics.py`](../../tests/gateway/test_run_progress_topics.py)
- Race-polish fixture coverage: [`tests/gateway/test_discord_race_polish.py`](../../tests/gateway/test_discord_race_polish.py)

This describes implemented-and-wired source behavior only. It is not live acceptance.

## Commit Boundaries

The preserved candidate has three atomic commits on top of source base `31bb2aed2f65d8d4193b97d57a11770009fba656`:

1. `0e6f2526a6324fff8ea1b0cd42e4eaeba629f534` — `fix(discord): restore voice session baseline`
2. `2311b967a0108f3d79859f583979cc0b48691bf4` — `feat(discord): speak sparse voice progress`
3. `fe99ce625b1ca46e60966fac857d516c1f35e430` — `test(discord): initialize generation state in concurrent join fixture`

The first commit is intended as an independently usable restoration checkpoint. The second adds revertible progress behavior. The third is a test-only CI fixture fix.

## Evidence Provenance

Historical source review and test evidence was gathered against the candidate lineage above:

- Independent review originally found blockers in alias-tail dispatch, manual/off lifecycle, and mute/progress gating. Parent inspection additionally found unwired interim speech; targeted independent re-review covered that correction.
- Corrections were made. The final review blocker was auto-departure erasing explicit `/voice off`; it was fixed by clearing auto-owned `voice_only` state without clearing `off`.
- Parent acceptance recorded 150 focused tests passing at `9718f1a138a44e9259aae2bb308b2a3819933196`, then 23 restoration tests after the final mute correction.
- Worker evidence recorded 23 restoration tests and 30 progress tests passing after the correction.
- `git diff --check` and added-line security scan were clean in the historical evidence.
- Hosted GitHub CI succeeded for exact candidate `fe99ce625b1ca46e60966fac857d516c1f35e430`: https://github.com/brianfaires/hermes-agent/actions/runs/34464455203
- The exact-SHA aggregate `All required checks pass` succeeded (job `102832422615`). Subsequent review independently ran final-candidate restoration and progress suites: 53 passed. These are historical execution results; documenting the branch does not claim a new test run or a CI run on the documentation commit.

Important limits on this evidence:

- The old 150-test invocation did not run at final SHA `fe99ce625b1ca46e60966fac857d516c1f35e430`.
- The initial hosted CI failure was a missing test-fixture map plus one intermittent relay concurrent-export SQLite lock. The fixture was fixed. The relay test later passed bounded local attempts, and full CI succeeded. The patch does not claim to fix the SQLite lock or prove it pre-existing.
- Evidence files and local optional workspace paths mentioned by the old artifacts, such as `ci-qualified-run.json`, `ci-qualified-jobs.json`, `voice-review.bundle`, task-local test homes, and task-local virtualenv paths, are provenance only and non-durable unless independently preserved.
- The historical source tree referenced by the old handoff was `72eefd2a8e9ff9120e4bbbebebc05b1302da1c71`; the accepted pre-integration source tree recorded in `ACCEPTANCE.md` was `2d3a29ca258c0ac135502aa6016a717ba1f45e0e`. Production paths matched; the final candidate adds only the test-fixture correction. Use commit SHAs above as the durable checkpoints.

## Not Yet Accepted Live

No live Discord runtime activation, provider STT/TTS calls, acoustic testing, or real channel conversation was performed. The staged code was not made active.

Unmeasured human criteria:

- Acoustic quality.
- End-to-end latency.
- First-spoken-progress timing.
- Interruption behavior in a real voice channel.
- Correct behavior under actual speakerphone echo conditions.

The design proposed interrupt-stop p95 ≤300 ms from confirmed speech onset and warm first meaningful speech median ≤2 s / p95 ≤4 s after end of speech. These are unproven targets, not delivered performance. Initial human checks: configured join/reconnect, one conversation roundtrip, an alias with arguments, meaningful interim speech before long work, sparse truthful progress, stop/new-utterance stale-audio cancellation, mute across departure/rejoin, and correct bot/profile ownership.

Deferred design work remains outside this patch:

- Full provider streaming.
- Streaming STT.
- Semantic classification of correction versus status/progress speech.
- Echo cancellation.
- Model/voice A/B evaluation.

## Practical Resumption Checklist

Resume only after explicit Brian authorization to resume Discord voice work.

1. Start from an isolated branch; do not use `main` or `staging` directly for repair work.
2. Inspect fresh `main` and relevant new development since `31bb2aed2f65d8d4193b97d57a11770009fba656`, including `cf62291dad2cd9c12de80bc5194ff76ef0f6ce55`.
3. Incorporate the Kanban worker prompt hotfix and any later development safely, preserving unrelated user work and avoiding force/reset workflows.
4. Re-review the affected diff against current source, not only against the historical base.
5. Re-run focused verification on the new SHA and expand coverage if merge conflicts, gateway behavior, voice mode state, or slash dispatch changed.
6. Requalify a new candidate SHA on staging only when authorized.
7. Treat runtime activation as a separate approval: use the existing independent Claude release lane, exact candidate, bounded authority/expiry and verified recovery coverage for the single canonical runtime. Perform a bounded human voice test; use the then-current known-good runtime/data/dependency state for rollback. Do not reset shared staging or restore the old base merely because an old handoff names it.
8. Do not imply or perform main promotion unless Brian separately approves it.

## What Remains Unknown

Whether the candidate still merges cleanly and behaves correctly after current `main` changes; whether live Discord voice meets Brian's acoustic, latency, and interruption expectations; whether provider-specific STT/TTS behavior exposes new edge cases; whether the intermittent relay SQLite lock still appears under CI load; and what exact runtime rollback point is valid at the time of any future activation.
