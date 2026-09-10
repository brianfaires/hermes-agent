# Independent native Codex correction review

Reviewer: `/root/ci_correction_review`, fresh context, read-only; parent preserved this returned report. Reviewed only `e36f3d76ff7464cf51a0a5881934e005fbbb9ee3..7abcbc9d82e4216da4242d75d599d43aafbb96fe` (six files), with the implementation brief and indexed CI failure evidence.

**No blocking findings.**

- Restart assertion updates retain exact supervisor/detached routing, single-call expectations, draining response, and no-interruption checks. They additionally require the caller’s delivery key and explicit delivery-token argument.
- The terminal preview fixture retains meaningful truncation pressure through 20 repeated flags. Assertions require the meaningful command, omit setup and subsequent commands, reject the complete long argument sequence, and preserve fenced-block formatting checks.
- ElevenLabs now imports `VoiceSettings` only when settings exist. Configured settings still use the same shared helper in synchronous and streaming paths; bounds, provider-specific speed precedence, global fallback, and output formats remain covered. The added regression exercises both entrypoints with SDK imports deliberately unavailable and verifies dotenv-key forwarding and omitted default settings.

Independent validation used the canonical interpreter read-only, `-B`, source `PYTHONPATH`, bytecode disabled, and a fresh environment containing temporary `HOME`, `HERMES_HOME`, `HERMES_BUNDLES_DIR`, `PATH=/usr/bin:/bin`, `TZ=UTC`, and `LANG=C.UTF-8`:

```text
/home/brian/.hermes/hermes-agent/.venv/bin/python -B -m unittest -v \
  tests/tools/test_v021_elevenlabs_settings.py \
  tests/agent/test_v021_compact_display.py \
  tests/gateway/test_v021_restart_delivery.py

Ran 12 tests in 1.708s — OK
```

`git diff --check` passed for the reviewed range.

The changed pytest fixtures were inspected but not executed locally; their validation remains with fresh hosted CI. This review does not establish hosted-CI readiness. No source, index, refs, configuration, canonical installation, or durable report files were modified by the reviewer. Closed restart R1/R2 findings were not reopened.

## Finding closure

No new acceptance findings were raised. Indexed failures have concrete source/assertion corrections in the reviewed commit and targeted local evidence. Hosted verification of those corrections remains pending; it is not represented as a closed green CI run.
