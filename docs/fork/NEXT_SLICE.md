# v0.21 retained-feature integration — 2026-09-07

Current release owner: Ang, task `t_ddd2e9dc`.
Candidate worktree: `/home/brian/.hermes/kanban/boards/operations/workspaces/t_ddd2e9dc/qualified`.
Candidate branch: `ang/t_ddd2e9dc-v021-qualified`.
Preserved implementation worktree/branch: sibling `repo`, `ang/t_ddd2e9dc-v021-integration`.
Doctor baseline: `9ccb53e3d15730fcae88b086ea954dfc377574aa`.

All five accepted source candidates are assembled with one cohesive commit each: FC-41 `/new`, its linear FC-42 `/log` child (no duplicate `/new`), FC-11 Calendar, FC-37 history (including static-bank compatibility), and FC-44 standalone controller (including frozen staging/main fast-forward compatibility). Documentation reconciliation is a separate commit. FC-43’s historical blanket journal block is SUPERSEDED by intentional finalized journal ingestion in the correct profile scope. Other feature source remains unchanged; all 157 historical inventory rows remain accounted for.

The upstream ancestor remains stable `v2026.8.31`, peeled `29112bef099274229cadff79cdff7bf7b99c4b77` (package version `0.21.0`). Earlier v0.19-base claims are superseded. A candidate branch name, source review or tests do not establish deployment, full rebase completion or feature activation.

## Current authorization, not readiness

Brian's renewed `OOB 4 hr` superseded the expired original eight-hour authorization. Receipt starts the clock; qualification or worker startup does not delay it.

| Boundary | Timestamp |
|---|---|
| Receipt/start | `2026-09-07T05:34:54-07:00` |
| Latest forward cutoff | `2026-09-07T09:04:54-07:00` |
| Hard expiry | `2026-09-07T09:34:54-07:00` |

This is ONE scoped retained-feature release batch, with necessary restart/verification, closing on success. The earlier repeated-release exception is not carried forward. Use an earlier cutoff if measured fencing/recovery needs more reserve. An incomplete in-flight batch must recover to the last accepted known-good; no deployment means no gratuitous rollback. This file never renews authorization or waives Critical, exact-SHA CI, backup, independent-controller or live-verification gates.

Brian cleared the credential incident as a source-release gate; Ops recovery remains separate. Google OAuth and Calendar activation/live Google UAT were explicitly deferred until 2026-09-08 and do not block source qualification/release. No browser consent, credential-refresh probe or claimed Calendar readiness is implied.

## Activation and execution boundaries

- FC-11: Ops owns sole-owner Calendar/grants/config and controlled result UAT. Deferred OAuth is not a passed activation gate.
- FC-37: scoped OS-owner CLI, NOT a chat slash command. Recorded evidence and selected current-memory reconstruction are distinct. Valid injective `{profile}` templates or explicit static `bank_id` / legacy `banks.hermes.bankId` are supported; malformed templates refuse without fallback. Shared-bank routing is not a per-profile ACL: authorization and complete-response session/profile provenance remain mandatory. No live compatibility or journal-entry session-reconstruction claim.
- FC-42: CLI/TUI and authorized Telegram/Discord delivered text only, not voice/media/desktop/split continuations. Historical schema/vault root `/home/brian/Documents/Projects/personal-history-log` was identified read-only; Ops still owns actual configuration, explicit inexpensive model and dedicated authorized journal-bank selection. No mass backfill, automatic deletion or accidental chat ingestion is authorized.
- FC-44: source/disposable qualification only. The live doctor baseline already has the v0.21 control socket. Its `pause-for-update` exits/restarts and is NOT stop-and-hold or completed all-consumer drain evidence. Actual startup/import/cache provenance, admission/source-consumer holds, real health observations, backup/restore evidence, independent recovery and runbook-aligned command packets remain required before cutover. Missing producer inputs must never be fabricated.

Ops discovery artifacts are attached to `t_907703d2`, `t_99835e41` and `t_891d8b21`. The final adapter report is limits evidence only: its named implementation/test files were not durably delivered and must not be installed or treated as independently reproducible qualification.

Candidate evidence is outside source in the task workspace: `frozen-candidate.json` (pre-documentation freeze), `frozen-test-results.json`, `frozen-static-evidence.json`, `final-source-review.md`, and the subsequent final release checkpoint. Source review is separate from exact-SHA hosted staging CI and deployment. Any documentation-only successor must record executable/test blob equivalence to the reviewed/tested head.

Observed qualification checkpoint: on `2026-09-07`, exact staged commit `32d416cf8d30a126cc9897b7a7f97dba019cec09` reached hosted staging CI run [34128183157](https://github.com/brianfaires/hermes-agent/actions/runs/34128183157) and failed before jobs were created (`0` jobs). The underlying cause was not available from that receipt and is not proven to be a product, YAML or billing failure. This docs-only successor is not that CI SHA and still needs exact-head staging CI before release. Canonical remains `9ccb53e3d15730fcae88b086ea954dfc377574aa`; no cutover has occurred. Source review PASS `ec6dc0af` applies with only docs-successor differences; history86/controller76 retained tests were inspected rather than rerun.

Do not collapse the remaining gates. Actual startup/cache/admission/drain/hold/fresh-health/restore/installed independent recovery gates remain separate. The governing runbook currently specifies stage `git switch staging` with the target already exact, while the controller performs the stopped switch followed by `git merge --ff-only` to the exact candidate SHA. Reconcile that exact-command mismatch before approving a frozen packet; never advance live checked-out staging early to satisfy it. The expiry-minus-abort reserve must be at least `max(1800, 3*R+10, measured end-to-end recovery)`, where `R` is the controller's configured recovery reserve in seconds. No governing-runbook edit or live staging move was performed in this qualification.

Ang owns final qualification and release under the current runbook. No source/ref/index/working-byte mutation in the live canonical checkout while its readers are running; no ref-only promotion, force push, permission bypass or unapproved service/config/credential change. Preserve old worktrees/refs and incident-held reconstruction graphs (`t_6d9a0f05`, `t_99b38c75` and descendants).
