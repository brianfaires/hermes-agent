# Independent Discord voice source review

Previous review verdict (before the newly reproduced idle/progress interaction): **no remaining concrete P0/P1 blockers in the accumulated scoped review at `4557bc60348250d464530d6cc63bdd19c59e48e8`.** This is source qualification, not live Discord/provider or acoustic acceptance.

## Targeted streaming-idle/progress follow-up

Scope: clean `bd75c81ffe2edfc72180dfadf07ec77f57eaa8f4` plus the correction's six-file Python delta, patch SHA-256 `76f050f61e591f229ca4c1840460862ff07e4bebd2aaa056907f6f2a485fa424`. The final correction commit includes this exact reviewed runtime/test delta and four source documentation updates; its exact post-commit SHA is in `../PROGRESS-FIX-RESULT.md`. Reviewer is independent native Codex child `/root/progress_invariant_review`, read-only; parent is sole source writer.

Parent independently reproduced two failed consumer/adapter invariants on unchanged production source: no idle-player release, and no truthful timer speech after initial streamed commentary. The reviewer limited follow-up to those failures and introduced lifecycle edges: delayed provider progress after tool completion/close, no-ambient mixer pause/resume/abort, zero-PCM clause reuse, and inactive-provider fallback. All identified corrections have focused executable regressions. Dequeue silence revalidation also covers the worker's queue-removal race. No unrelated audit, ref/config/runtime writes, or provider calls were requested from the reviewer.

**Final scoped verdict: ACCEPT, no remaining concrete P0/P1 or acceptance blocker.** Reviewer independently ran `HERMES_PYTHON=$PWD/../test-venv/bin/python PYTHONPATH=$PWD scripts/run_tests.sh tests/gateway/test_discord_voice_stream.py -q` on the frozen delta: **19 passed, 0 failed**, exit 0 (6.2 seconds). Parent additionally passed the stream/progress subset: **49 passed, 0 failed**, two files, `../progress-final-focus-qualified.log`. Parent final relevant subset on this exact Python delta: **326 passed, 0 failed, no skips**, 24 files (`../progress-qualified-broad.log`, 84.3s). Six Python ASTs, reviewed-delta checksum, whitespace and document checks passed. Standalone native review artifact: `../PROGRESS-FIX-REVIEW.md`. Earlier native runs caught fixture-only omissions in the reused gateway harness (runtime callback attributes and turn-generation metadata); they are preserved in the task evidence rather than presented as successful runs.

## Historical review provenance

Reviewer: independent native Codex child `/root/compatibility_review`, read-only, inherited model/tier settings; parent `/root` was the sole source writer. Reviewer made no edits, ref changes, provider calls or live mutations. Tests below were executed by the parent, not independently rerun by the reviewer.

## Exact scope and findings

1. Compatibility: automatic merge of restoration `d06c1ece5da289f0295ea7ed7d862bb15328b5a4` into voice `31ee83fe4bab0f11a5e46841890229844c32b9d8`, subsequently committed as `320fc6db370c72cf15fa21f7ef2ea2f5b83777fd`. Reviewed Discord adapter overlap, gateway callback ownership, authorization/profile routing, slash/base interactions and fixtures. No concrete P0/P1 compatibility blocker; unrelated restoration changes were not broadly re-audited.
2. Voice deltas: timing/onset commits `42f80b293b2e49f53f527a0ee1cfece9daf145fe`, `51c04a1646dcc16a91f095f0b461f66ebebf4aa3`, and the PCM working diff later committed as `aae5ce1c963046c6545dd8614f0e0a04fbb350ee`. Found two P1s: older STT could acquire the newer output epoch; partial first-chunk PCM writes could permit full-final replay. Both fixed and covered by delayed-STT and first-write-partial-failure tests.
3. Controls: `aae5ce1c963046c6545dd8614f0e0a04fbb350ee..347d8b0c33419c9f13957d558656e7ec8eab7406`. Reviewed deterministic status/stop/correction, normal authorization, awaiting idle stop, replacement ordering, isolated latch note, and noncanceling status. No additional P0/P1 in controls. Targeted stale-input re-review identified an await gap at opt-in transcript echo.
4. Echo correction: `347d8b0c33419c9f13957d558656e7ec8eab7406..1aa651d22815516e52870547b862d15a059817bd`. Callback-entry epoch snapshot and post-echo check close the gap before latch consumption or adapter dispatch. Regression cases cover original and replacement transcript callbacks. No remaining scoped P0/P1 at this SHA.
5. Final parent-found reconnect variant: `1aa651d22815516e52870547b862d15a059817bd..4557bc60348250d464530d6cc63bdd19c59e48e8`. Successful new voice connection now advances output epoch beside existing session epoch. Reviewer confirmed it invalidates pending departed-connection response tasks. Regression executes real join with a controlled transport and rejects old-scope playback. Final accumulated verdict remains clear.

After the initial reviews, follow-up reviews were confined to new controls and failed invariants; no recursive broad audit was performed.

## Executable evidence considered

- At review time, parent had reported a 24-file 312-pass subset from the preceding source. After final review, with runtime source frozen at `4557bc60348250d464530d6cc63bdd19c59e48e8`, parent reran the same 24 files: **313 passed, no failures or skips**. No source change followed the reviewed reconnect guard.
- Preceding source `1aa651d228`: real local NaCl/Opus packet-flow integration **34 passed**, no Discord connection or live provider.
- Final source `4557bc6034`: changed reconnect invariant plus onset/restoration/concurrent-join coverage **35 passed**.
- Earlier focused evidence: stream/consumer/onset25, controls/mixer/provider82, status/onset16, delayed-echo39, and voice-command/restoration106. Counts overlap; they are not additive coverage totals.
- Parent static checks: changed Python AST parsing, added-line security inspection and `git diff --check`. Ruff was unavailable.

The historical reviewer inspected implementations and regression tests but did not independently execute pytest; the bounded follow-up reviewer independently passed the 19 streaming cases as recorded above. Parent's canonical runner used controlled external boundaries, isolated test homes and task-local dependencies; no pytest stubs. No claim that mocks establish latency, provider compatibility, acoustic quality, p95 targets or human acceptance. Full shared-staging exact-SHA CI and live UAT remain later gates.
