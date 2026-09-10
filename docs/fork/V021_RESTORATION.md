# v0.21 restoration source handoff

**Partial source restoration; not CI-ready or live-qualified.** Work is local on `ang/v021-restoration-t_4a31a31b`, based on `cf62291dad2cd9c12de80bc5194ff76ef0f6ce55`. Ang owns independent integration and hosted CI. No main/staging/remote writes, live installation, restart, delivery, configuration, credentials or Kanban operations occurred. The canonical launcher remains `/home/brian/.hermes/hermes-agent/.venv/bin/hermes`.

[The ledger](V021_RESTORATION_LEDGER.json) records all **51 unique audit IDs: 37 old-main and 14 archive-only**, preserving all 239 historical evidence entries and distinguishing mixed subfeatures. It includes old/current source anchors, baseline blob identity, runtime-test paths, limitations and exact owning commit hashes. The September 10 audit and CSV are unchanged. A source anchor alone is not runtime parity; unresolved retained claims remain qualification blockers.

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

The canonical interpreter lacks `pytest`. The attempted prescribed pytest path collected **zero tests**, explicitly recorded as a blocker; no alternate venv or installation was used. Added standard-library unittest tests live under `tests/` for hosted pytest collection, but actual hosted collection/CI remains unrun.

Focused receipt: 17 new test files / **50 tests passed** at `10081c3099c814c8e5f8c6e0f8f83d2b9881ac40`, then **five restart tests passed** after the final busy-reply fix `a71449a566` (one additional case). No full local suite or live transport/provider tests ran. Test pattern:

```sh
env -i HOME=<task-temp> HERMES_HOME=<task-temp>/hermes PATH=/usr/bin:/bin \
  PYTHONPATH=<worktree> PYTHONDONTWRITEBYTECODE=1 TZ=UTC LANG=C.UTF-8 \
  /home/brian/.hermes/hermes-agent/.venv/bin/python -B -m unittest discover \
  -s <test-directory> -p <test-file> -v
```

Detailed task-local receipts are `../qualification-final.json`, `../fc36-r2-green.txt` and the slice evidence reports. Native independent Codex review is separately preserved in `../independent-review.md`; its findings are never replaced by the implementation summary. `../implementation-result.md` records final hashes, checks, review outcome and launcher/status readback. `git diff --check` and an added-line security scan are required final handoff checks; their receipts are in the parent evidence folder.

## Decisions and remaining qualification

Precise unimplemented decisions are in `../critical-gates.md`; they require Brian's explicit approval before affected code changes:

- FC17/19: pairing/transport identity, profile credentials/routing/config precedence, durable owner recovery, webhook namespaces, cron registry/shutdown identity and notification recipients/policy.
- FC18/11: persistent-directory branch/dashboard interface and stricter normal-write/name/toolset validation compatibility.
- FC14/15: literal markdown, standalone-only MEDIA interpretation and configured free-response auto-thread routing.
- FC25: rollback of an already-published compression child without violating newer lease/concurrent-tail safeguards.
- FC26/27/32: larger default skill descriptions, model execution/cost guidance and unconditional test-thread ceilings.
- FC28/30/33: pip-compatible voice crypto packaging, CI publication/process-guard policy and Tirith verdict filtering.

FC02 launcher guards, tools-config UI, standalone Discord REST/control/thread ancestry, broader delegation/compression/profile fixtures and other ledger-listed retained paths still need executable qualification under an approved test environment. Missing pytest is a real environment blocker, not evidence of parity. Windows process-tree cleanup remains untested locally.

Discord voice (FC24 and voice-specific FC17) stays deferred; the parked source was not reused. Intentional policy/interface replacements and archive-only features remain excluded. There is no blanket retirement of unintended losses: unfinished subfeatures retain explicit blockers and next steps in the ledger.

## Integration and rollback boundaries

Use the ledger's exact local commits; do not infer ancestry as behavioral parity. Source slices are independently revertible where indicated, preserving unrelated work. The FC36 callback and busy-reply follow-ups belong with its initial restoration. FC25's final commit also removes one unused webhook-test import due to a disclosed shared-workspace amend race; no source was lost. Dependency rollback includes both manifest and lockfile.

Next: Brian decides gated slices; Ang supplies an approved pytest/CI environment, runs focused existing sibling suites plus hosted checks against the exact integrated candidate, and resolves any concrete failures. Only then prepare candidate-bound human verification. Live activation and main promotion remain separate actions; this handoff authorizes neither.
