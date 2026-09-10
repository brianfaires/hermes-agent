# v0.21 restoration source handoff

**Restored safe slice: correction candidate ready for Ang to push fresh staging CI; root initiative remains blocked on precise Critical decisions.** Source candidate is `7abcbc9d82e4216da4242d75d599d43aafbb96fe`, recorded before this documentation commit. The final documentation-bound candidate is the branch HEAD reported at handoff. Branch: `ang/v021-restoration-t_4a31a31b`; base: `cf62291dad2cd9c12de80bc5194ff76ef0f6ce55`.

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

[Hosted run 34521202771](https://github.com/brianfaires/hermes-agent/actions/runs/34521202771) exercised `e36f3d76ff7464cf51a0a5881934e005fbbb9ee3`. All **17 new test files / 51 tests passed actual pytest collection and execution**. Ang also independently reran those 51 locally. Exact per-file receipts and eight Python slice summaries are preserved in [V021_CI_EVIDENCE.json](V021_CI_EVIDENCE.json). This was a failing run: eight Python-test failures plus one e2e failure, not a green CI attestation.

Correction `7abcbc9d82` addresses all nine indexed cases: seven exact restart mocks now require delivery keys/signals while retaining supervisor/drain checks; the fenced progress fixture expects a meaningful shell command and keeps long-line truncation pressure; ElevenLabs lazily imports optional SDK types only when configured values require them. The original historical helper imported eagerly too; this is a concrete no-settings regression against the retained provider path, not blind historical copying. Configured bounds and sync/streaming behavior remain intact.

The new no-settings/no-SDK test failed before the source correction for all three default configurations. After correction, **12 focused unittest tests passed** (TTS 4, compact display 3, restart delivery 5), and a fresh native read-only Codex reviewer independently passed the same 12. The original 51 tests remain, with one added regression. The [correction review](CI_CORRECTION_REVIEW.md) found no blockers; the [original independent review](independent-review.md) is preserved verbatim, including closed R1/R2.

The canonical interpreter lacks pytest. That is a local host limitation, not an approval blocker. Changed existing pytest fixtures await a fresh hosted run; no installs or broad local test loop were performed. Local tests used the canonical interpreter read-only with bytecode disabled, source PYTHONPATH and fresh temporary HOME/HERMES_HOME/HERMES_BUNDLES_DIR. Task receipts: `../ci-correction-red.txt`, `../ci-correction-tests.json`, `../ci-correction-evidence.md`. Historical qualification receipts retain their original time-bound limitations.

## Decisions and remaining qualification

Precise unimplemented decisions are in [critical-gates.md](critical-gates.md); they require Brian's explicit approval before affected code changes:

- FC17/19: pairing/transport identity, profile credentials/routing/config precedence, durable owner recovery, webhook namespaces, cron registry/shutdown identity and notification recipients/policy.
- FC18/11: persistent-directory branch/dashboard interface and stricter normal-write/name/toolset validation compatibility.
- FC14/15: literal markdown, standalone-only MEDIA interpretation and configured free-response auto-thread routing.
- FC25: rollback of an already-published compression child without violating newer lease/concurrent-tail safeguards.
- FC26/27/32: larger default skill descriptions, model execution/cost guidance and unconditional test-thread ceilings.
- FC28/30/33: pip-compatible voice crypto packaging, CI publication/process-guard policy and Tirith verdict filtering.

Twelve qualification-only entries now cite concrete retained tests and exact hosted per-file outcomes: doctor/setup worktree launcher guards; tools-config memory auto-enable/explicit-disable; Discord profile snapshot/thread ancestry/standalone REST/control egress; delegated child construction, environment lineage and schema exclusion; general spoken-content compression, protected handoff replacement and durable rotation. These attest only to named assertions, not blanket historical equivalence. Related profile scope/lifecycle and restart plugin results are supplemental where they do not cover the full historical claim.

Remaining source-only uncertainty includes install.sh's separate launcher path, full command/profile callback and cron ticker qualification, restart all_profiles preflight and teardown details, and the historical progress/profile fixture contract. Preserved FC17 paths are not automatically new Critical changes: read-only qualification needs no Critical approval. Actual missing identity/trust/routing/compatibility changes stay behind the precise gates. The corrected existing progress/drain fixtures still require fresh hosted results. Windows transform process-tree cleanup remains locally untested.

Discord voice (FC24 and voice-specific FC17) stays deferred; the parked source was not reused. Intentional policy/interface replacements and archive-only features remain excluded. There is no blanket retirement of unintended losses: unfinished subfeatures retain explicit blockers and next steps in the ledger.

## Integration and rollback boundaries

Use the ledger's exact local commits; do not infer ancestry as behavioral parity. Source slices are independently revertible where indicated, preserving unrelated work. The FC36 callback and busy-reply follow-ups belong with its initial restoration. FC25's final commit also removes one unused webhook-test import due to a disclosed shared-workspace amend race; no source was lost. Dependency rollback includes both manifest and lockfile.

Ang may push the final exact candidate to fresh source-only staging CI and verify the four changed pytest files plus the dotenv sibling file. No correction CI pass is claimed yet. Brian's decisions remain prerequisites only for the affected gated changes; the root initiative is not whole-scope human-ready. See the [candidate-bound safe-slice checklist](V021_SAFE_SLICE_CHECKLIST.md). Activation and main promotion each require separate authorization.
