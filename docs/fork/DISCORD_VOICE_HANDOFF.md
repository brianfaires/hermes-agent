# Discord voice source handoff

All six scoped source milestones are implemented, source-qualified and published on isolated `dev/discord-voice`. The earlier parked/MVP-only status is superseded. No deployment or live acceptance occurred.

Corrected source candidate: `c1219b13de8d685fd52f4a315a63765888894a1f`, tree `8bbcf45a0a8fb059aea945ce8a8000fdcae2d045`. The earlier candidate is superseded for the confirmed milestone 2/5 idle/progress interaction. [Milestone ledger](DISCORD_VOICE_MILESTONES.md) records feature commits, safety corrections, published annotated RC tags, evidence, limitations and rollback boundaries. [Independent review](DISCORD_VOICE_REVIEW.md) is source-scoped. [Human UAT](DISCORD_VOICE_UAT.md) remains entirely unchecked. Subsequent publication-documentation commits do not change runtime or test code.

## Delivered source

- Retained accepted join/reconnect/manual leave/off, owning-profile receive/reply, spoken aliases with argument tails and normal authorization, and utterance cancellation.
- Retained first-tool acknowledgment, actual interim commentary and sparse truthful progress, with PCM/stale-output coordination.
- Added bounded local timestamp-only diagnostics; no raw payload/identifier logging.
- Kept receive live during playback and stopped stale output on confirmed authorized mapped speech before STT completion. Epoch checks cover delayed synthesis/decode, STT, queued inputs, optional transcript echo and replacement connections.
- Implemented the existing streaming consumer's Discord PCM contract, format conversion, one-second queue, cancellation, mixer integration and conservative duplicate-final suppression after partial output.
- Added exact spoken status/stop and “correction: …” controls through normal session dispatch. Status leaves work running; correction awaits authorized stop before submitting the replacement. Existing `/steer` aliases retain their normal behavior.

Current source anchors: `plugins/platforms/discord/{adapter,voice_output,voice_stream,voice_timing,voice_mixer}.py`, `gateway/{run,slash_commands,streaming_tts_consumer}.py`, and `tools/tts_streaming.py`. No new model tool or front runtime was added.

## Streaming idle/progress correction

The opening clause now drains to EOF before a long tool wait. Sparse truthful timer progress uses the same consumer and turn handle as model clauses, with no competing whole-file playback. No-ambient mixers pause during those waits; configured ambient remains intentional. Delayed progress rechecks tool/turn/connectivity state before PCM writes. Final/partial/cancel suppression is retained, and subsequent mixer file/ack speech resumes normally. Regressions exercise real gateway callbacks, consumer, adapter, PCM reader and mixer with controlled provider/transport boundaries.

All six annotated `progressfix-rc2` source tags in the ledger were published and remotely verified by Ang, including each tag object and peeled commit. They target cumulative corrected `c1219b13de8d685fd52f4a315a63765888894a1f`; original feature SHAs remain provenance. No tag was moved or deleted.

## Verification and limits

Corrected candidate: **326 passed, 0 failed, no skips**, the relevant 24-file subset, independently rerun by Ang on committed `c1219b13de` (`../parent-final-tests.log`, 83.7s). Ang verified the committed Python delta matches the independent review's SHA-256 `76f050f61e591f229ca4c1840460862ff07e4bebd2aaa056907f6f2a485fa424`; added-line security scan and diff whitespace pass. The native reviewer accepted that delta and independently passed all 19 streaming regressions. Human acoustic UAT remains entirely unchecked.

Historical qualification at prior frozen source `4557bc60348250d464530d6cc63bdd19c59e48e8`: the 24-file relevant subset passed all 313 tests, with no failures/skips. Real local NaCl/Opus packet integration passed 34 tests at preceding source `1aa651d22815516e52870547b862d15a059817bd` without a Discord connection. The final reconnect-only change also passed its focused 35-test subset. Exact review/test provenance is in the ledger rather than implied to cover untested SHAs. Added-line review, Python AST parsing and diff whitespace checks passed; Ruff was unavailable.

No live Discord/provider tests, acoustic measurements or selected-provider/voice changes were made. Endpoint tuning, streaming STT comparisons, echo cancellation and model/voice A-B remain measurement-dependent. A front runtime remains conditional and unneeded without measurements. Proposed latency targets are unproven.

## Repository and continuation boundaries

- Starting voice HEAD:`31ee83fe4bab0f11a5e46841890229844c32b9d8`.
- Compatibility merge:`320fc6db370c72cf15fa21f7ef2ea2f5b83777fd`, merging existing restoration `d06c1ece5da289f0295ea7ed7d862bb15328b5a4` into voice only, preserving both histories.
- Read-only remote checks: main`cf62291dad2cd9c12de80bc5194ff76ef0f6ce55`; staging`d06c1ece5da289f0295ea7ed7d862bb15328b5a4`. Neither shared ref was changed.
- Voice branch and six new annotated milestone tags pushed atomically and read back exactly. Tags describe source RCs, never live releases. Shared main/staging remained unchanged.
- Current CI push triggers support main/staging, not voice refs. No policy changes or staging movement were used to obtain CI. Exact integration-SHA hosted CI is a later gate.
- No canonical checkout/config/credential/runtime/global installation changes. Isolated task test-venv uses source-first paths, read-only existing dependencies, and repository-pinned NumPy installed locally.

The source scope is complete, verified and published. Remaining gates are separately authorized integration/CI/activation and unchecked human UAT. Runtime activation must use the existing independently authorized release procedure and then-current proven rollback; old source bases are not runtime rollback orders. Do not promote main or reset shared staging based on this handoff.

Task-local durable evidence lives beside the checkout: `../OVERNIGHT-CHECKPOINT.md`, `../OVERNIGHT-EVIDENCE.md`, `../OVERNIGHT-RESULT.md`, and `overnight-*.log`. These are recovery aids; this repository's ledger, review, UAT and exact commits are the retained handoff. Parent updates external plan/Obsidian statuses. Historical milestone 1/2 CI and reviews remain provenance in the ledger; old task artifacts do not authorize activation.

Correction evidence: `../PROGRESS-FIX-EVIDENCE.md`; final native review: `../PROGRESS-FIX-REVIEW.md`; exact commit/tag mapping and final summary: `../PROGRESS-FIX-RESULT.md`. These task-local artifacts supplement the retained source ledger/review/UAT; no unrelated audit was reopened.
