# Current checkpoint: finite pytest/CI compatibility correction

Hosted **34547488980 completed FAILURE** for `75a30e9809`, with26 indexed
cases across six Python shards plus the aggregate. This supersedes previous
starting/queued status. Source `7581ed1325` was preserved without staging
integration. All approved scope remains implemented; this is a finite correction
of actual product/fixture contracts and pytest compatibility, with no new features.

[Correction evidence](V021_APPROVED_FINAL_CORRECTION.md),
[actual pytest receipts](V021_APPROVED_FINAL_CORRECTION_TESTS.json) and
[independent review](V021_APPROVED_FINAL_CORRECTION_REVIEW.md) bind the corrected
candidate. Prior reviewed receipts remain immutable. The cumulative human checklist
remains78 IDs, all pending; no additional UAT gate. Ang owns staging integration/push
and fresh exact-candidate hosted CI. Workers do not push; main/live remain excluded.

---

# Current checkpoint: approved batch 3 locally qualified

All 26 remaining approved IDs plus G32 are implemented and independently reviewed.
[Batch 3 evidence](V021_APPROVED_BATCH3.md), [independent reviews](V021_APPROVED_BATCH3_REVIEW.md)
and [raw checks/bindings](V021_APPROVED_BATCH3_TESTS.json) bind the exact source candidate.
No scoped implementation remains pending. The single cumulative checklist covers
78 landed IDs in 36 grouped rows, all human results pending. Critical labels and
voice/retired/archive exclusions remain. First-batch CI failed; fresh exact-candidate
hosted CI remains required. Ang may integrate/push staging; workers do not push;
main/live remain excluded. Prior checkpoint counts and stop/next-step language below
are historical and superseded by this checkpoint; prior receipts remain immutable.

---

# Current CI correction checkpoint

First-batch CI **34545497788 completed FAILURE** for `4cef65cada`: one failing
execution-ledger profile fixture plus aggregate, per Ang's completed-run report.
[Correction decision and qualification](V021_APPROVED_CI_CORRECTION.md) supersede
previously reported queued status. Batch 2 production remains unchanged; this
narrow correction strengthens the real store/profile test. **Fresh exact-candidate
hosted CI is required** after Ang integrates batch 2 plus correction. No push,
live changes or remaining feature work in this bounded turn; stop quiescent for
Ang's independent verification. Existing human/pending feature counts unchanged.

---

# Current approved checkpoint — batch 2

[Batch 2](V021_APPROVED_BATCH2.md) continues accepted `4cef65cada` with source
candidate `59b7ab83634cdf1e57c091e9072002b18b7eba3b`: 15 implemented nonvoice G17
subfeatures and one compatible retained executor qualification. Independent review
passed all 36 new tests with no P0/P1 blockers. Cumulative human checklist: 52 landed
IDs, all pending; 26 approved ledger entries plus G32 thread contract remain.
Next: G17 served-profile ownership/status E17/E18/E29. Existing approval stands;
Ang handles staging/CI. No push or live mutation. Prior checkpoints below remain
historical; their older counts and next-step statements are superseded here.

---

# Approved restoration continuation

Brian's 2026-09-10 staging approval supersedes historical implementation holds
in this document, critical-gates.md, and prior continuation/tail records.
Implementation is in progress; human UAT stays deferred and never blocks dev.
Critical classifications remain. G17R is the first approved implementation batch;
exact commits, tests and remaining steps are recorded in
[V021_APPROVED_CONTINUATION.md](V021_APPROVED_CONTINUATION.md) and the ledger.
Workers may not push or integrate staging; Ang is authorized to integrate and push
staging and owns exact-SHA hosted CI. Main and live changes remain excluded.

The previous handoff below is preserved as a historical checkpoint. Its parked,
approval-required and next-integration statements are superseded by this section;
its actual CI results remain valid only for their named commits.

---

# v0.21 restoration source handoff

**Bounded continuation: source qualification complete; human UAT deferred; affected Critical slices remain parked.** Test/source candidate is `76d908a34804889c9c27cebd08619e71528070e4`, on branch `ang/v021-restoration-t_4a31a31b`, continuing from `3c8f7d0d160883dc48a169d12f7e3cdca203b58d`. Final documentation-bound HEAD is recorded in the continuation handoff.

Brian's continuation direction supersedes the earlier partial-milestone stop: continue separable safe development and grow the human checklist. Human UAT is not a development prerequisite. No main promotion, activation or live mutation is authorized by this qualification.

Ang previously pushed exact reviewed tree `e36f3d76ff7464cf51a0a5881934e005fbbb9ee3` to source-only staging. This correction batch made no staging/main/remote writes, live installation, restart, delivery, configuration, credential or Kanban changes. The canonical launcher remains `/home/brian/.hermes/hermes-agent/.venv/bin/hermes`.

[The ledger](V021_RESTORATION_LEDGER.json) records all **51 unique audit IDs: 37 old-main and 14 archive-only**, preserving all 239 historical evidence entries and distinguishing mixed subfeatures. It includes old/current source anchors, baseline blob identity, runtime-test paths, limitations and exact owning commit hashes. The September 10 audit and CSV are unchanged. A source anchor alone is not runtime parity; unresolved retained claims remain source qualification uncertainty, distinct from Critical approval gates.

## Implemented safe slices

- FC08: legacy directory records cannot authorize recursive deletion; stronger wildcard-root, symlink and durable-path restrictions remain.
- FC13: existing strict scanner covers effective inline/file prompts through core create/update, tool/API and skill-attached fire-time reload. Invalid API updates return 400 without persistence.
- FC18/20: inventory reads actual board databases despite worker pins; delegated/cron turns do not consume worker nudges, budget lifecycle, heartbeat/comment state or worker-only skill offers. The baseline task-plus-tool prompt hotfix is runtime-qualified.
- FC04/26: configured ElevenLabs controls reach sync/streaming SDK calls; new prompt builds refresh external skill manifests, with explicit description bounds. Existing conversation prompt bytes stay frozen; default description length stays 60 pending a cost decision.
- FC12/14/19: cron attention markers and correctly scoped thread diagnostics; compact tool displays and Discord progress embed suppression; notifier traceback diagnostics. Notification destinations and Discord threading policy are unchanged.
- FC36: restart awaits caller response delivery, including busy inline `/restart` replies. The existing authorization, audit denial, cooldown and work drain remain. Independent review found callback-timeout and busy-command races; both have dedicated fixes and real adapter regressions.
- FC25: manual compression cannot report success or clear token bookkeeping when no durable rotation/in-place compaction occurred.
- FC10/30/32: retained webhook transforms clean up their own process group on timeout; failed test-process spawning removes its temporary root.
- FC28: base installs regain the qualified Starlette selection and multipart floor. `uv lock` regenerated successfully; offline validation passes and all third-party lock entries, including versions and hashes, match baseline. No package was installed.

Runtime checks also cover retained browser family fallback, external memory injection, Langfuse multiline neutralization, selected Discord denial paths, slash-sync retries and background completion opt-in. See each ledger subfeature for its exact coverage; these are not blanket subsystem attestations.

## Verification and independent review

**New source-staging checkpoint:** Ang reports exact `eda8d0537b1b66000b5ade38855343634ddd4e75` was inspected, seven tests independently rerun, and fast-forwarded/pushed to source-only staging. Hosted CI is pending until an actual result is supplied. The [finite compatibility tail](V021_COMPATIBILITY_TAIL.md) completes G28/G30 prerequisite qualification and preserves all Critical gates. This documentation-only tail has not been pushed.

**Latest accepted checkpoint:** exact `3c8f7d0d160883dc48a169d12f7e3cdca203b58d` [run 34523897060](https://github.com/brianfaires/hermes-agent/actions/runs/34523897060) succeeded, aggregate SUCCESS, 28 successful jobs and six skipped. All 52 added tests passed. [Final previous CI receipt](V021_FINAL_CI_3c8f7d0.json) supersedes the pending correction status below; earlier failures/reviews remain historical checkpoints. [Continuation receipts](V021_CONTINUATION_RECEIPTS.json) bind seven new local tests and reused exact-file evidence to their candidate. Native review of only the new delta is preserved at [CONTINUATION_REVIEW.md](CONTINUATION_REVIEW.md).

### Historical checkpoints

[Hosted run 34521202771](https://github.com/brianfaires/hermes-agent/actions/runs/34521202771) exercised `e36f3d76ff7464cf51a0a5881934e005fbbb9ee3`. All **17 new test files / 51 tests passed actual pytest collection and execution**. Ang also independently reran those 51 locally. Exact per-file receipts and eight Python slice summaries are preserved in [V021_CI_EVIDENCE.json](V021_CI_EVIDENCE.json). This was a failing run: eight Python-test failures plus one e2e failure, not a green CI attestation.

Correction `7abcbc9d82` addresses all nine indexed cases: seven exact restart mocks now require delivery keys/signals while retaining supervisor/drain checks; the fenced progress fixture expects a meaningful shell command and keeps long-line truncation pressure; ElevenLabs lazily imports optional SDK types only when configured values require them. The original historical helper imported eagerly too; this is a concrete no-settings regression against the retained provider path, not blind historical copying. Configured bounds and sync/streaming behavior remain intact.

The new no-settings/no-SDK test failed before the source correction for all three default configurations. After correction, **12 focused unittest tests passed** (TTS 4, compact display 3, restart delivery 5), and a fresh native read-only Codex reviewer independently passed the same 12. The original 51 tests remain, with one added regression. The [correction review](CI_CORRECTION_REVIEW.md) found no blockers; the [original independent review](independent-review.md) is preserved verbatim, including closed R1/R2.

The canonical interpreter lacks pytest. That is a local host limitation, not an approval blocker. The earlier pending-fixture statement is superseded by successful exact-SHA run 34523897060; no installs or broad local test loop were performed. Local tests used the canonical interpreter read-only with bytecode disabled, source PYTHONPATH and fresh temporary HOME/HERMES_HOME/HERMES_BUNDLES_DIR. Task receipts: `/home/brian/.hermes/kanban/boards/engineering/workspaces/t_4a31a31b/ci-correction-red.txt` (task-only provenance), `/home/brian/.hermes/kanban/boards/engineering/workspaces/t_4a31a31b/ci-correction-tests.json` (task-only provenance), `/home/brian/.hermes/kanban/boards/engineering/workspaces/t_4a31a31b/ci-correction-evidence.md` (task-only provenance). Historical qualification receipts retain their original time-bound limitations.

## Decisions and remaining qualification

Precise unimplemented decisions are in [critical-gates.md](critical-gates.md); they require Brian's explicit approval before affected code changes:

- FC17/19: pairing/transport identity, profile credentials/routing/config precedence, durable owner recovery, webhook namespaces, cron registry/shutdown identity and notification recipients/policy.
- FC18/11: persistent-directory branch/dashboard interface and stricter normal-write/name/toolset validation compatibility.
- FC14/15: literal markdown, standalone-only MEDIA interpretation and configured free-response auto-thread routing.
- FC25: rollback of an already-published compression child without violating newer lease/concurrent-tail safeguards.
- FC26/27/32: larger default skill descriptions, model execution/cost guidance and unconditional test-thread ceilings.
- FC28/30/33: pip-compatible voice crypto packaging, CI publication/process-guard policy and Tirith verdict filtering.

Twelve qualification-only entries now cite concrete retained tests and exact hosted per-file outcomes: doctor/setup worktree launcher guards; tools-config memory auto-enable/explicit-disable; Discord profile snapshot/thread ancestry/standalone REST/control egress; delegated child construction, environment lineage and schema exclusion; general spoken-content compression, protected handoff replacement and durable rotation. These attest only to named assertions, not blanket historical equivalence. Related profile scope/lifecycle and restart plugin results are supplemental where they do not cover the full historical claim.

The ten remaining qualification-only entries are now resolved to the named assertions in [continuation qualification](V021_CONTINUATION_QUALIFICATION.md): launcher guard, delayed callback, scope reset, service enumeration, cron startup/heartbeat, secondary notifier map, obsolete progress fixture, restart preflight and graceful teardown. Seven new tests passed locally; directly matched existing hosted receipts are reused. This does not approve missing identity/trust/routing/compatibility changes. Windows transform process-tree cleanup remains locally untested.

Discord voice (FC24 and voice-specific FC17) stays deferred; the parked source was not reused. Intentional policy/interface replacements and archive-only features remain excluded. There is no blanket retirement of unintended losses: unfinished subfeatures retain explicit blockers and next steps in the ledger.

## Integration and rollback boundaries

Use the ledger's exact local commits; do not infer ancestry as behavioral parity. Source slices are independently revertible where indicated, preserving unrelated work. The FC36 callback and busy-reply follow-ups belong with its initial restoration. FC25's final commit also removes one unused webhook-test import due to a disclosed shared-workspace amend race; no source was lost. Dependency rollback includes both manifest and lockfile.

Ang has completed the continuation inspection, seven-test rerun and exact eda8d05 staging integration. Next inspect/integrate this documentation-only tail and verify actual exact-SHA hosted CI; no repeat of accepted testing is required by this tail. The previous correction **did pass** exact `3c8f7d0` CI; that receipt does not attest to new test collection. No further ungated production restoration was identified in the existing ledger. Critical decisions apply only to their affected slices; human UAT stays deferred and cumulative. See the [safe-slice checklist](V021_SAFE_SLICE_CHECKLIST.md). Activation and main promotion each remain separately unauthorized.
