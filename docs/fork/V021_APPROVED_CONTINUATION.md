# Approved restoration continuation

Brian approved the documented remaining restoration slices for source/tests/staging
on 2026-09-10 (root Kanban comment 16:51, recorded in the task's approved brief).
This supersedes older approval holds, without lowering Critical classifications.
Human UAT is deferred and never gates development. Ang owns staging integration,
push and exact-SHA hosted CI. No canonical/main edits, live state, installs,
activation, restart, delivery, host changes or Kanban writes occurred.

This is a bounded implementation batch, not completion of the restoration.
The ledger retains every historical audit ID/evidence entry and distinguishes
approved pending implementation from actual source/test evidence. Voice,
retired consumers and archive-only new features remain excluded. In particular,
FC-17-E28's historical branch-only lookup is not approved as an archive feature.

## G17R — profile-qualified cron registry

Pre-mutation contract: capture `(resolved cron store home, job ID)` throughout
registration/release/future ownership. Preserve the legacy global raw-ID getter,
add explicit current-store filtering for manual checks and recovery, and use a
qualified global snapshot for drain counts. A worker must release its captured
home after leaving copied context. Reconcile only the matching profile ledger;
keep live-future, previous-run, finite-repeat, execution-token and fire-owner
fences. No pool, credential or destination changes.

Implemented in `d97ba560254c93c14704b13b64fbdd5bb31ecd6a` (source tree
`ab4c7a9e1829de96a74760f76d78a9bb6dff66ab`). The execution ledger now follows
explicit cron-store overrides, matching jobs. A native reviewer found one
missed sibling liveness reader in jobs.py; the correction scopes both recurring
error recovery and exhausted one-shot cleanup to the current store. The final
review found no remaining P0/P1 blockers and binds all source/test file hashes.

Eight new real-entrypoint tests in `tests/cron/test_v021_profile_registry.py`
passed under isolated canonical Python: equal-ID dedupe/filter/count/release;
real tick dispatch and callback release outside copied context; explicit-store
ledger reconciliation with live-future preservation; exact-owner shutdown
interruption with peer protection; manual-run dedupe and release; ownerless
interruption lifecycle; recurring-error recovery; exhausted one-shot cleanup.
Transport/model bodies are fakes, real jobs/claim/execution stores are temporary.
No scheduler daemon or real shutdown ran.

Existing private-registry fixtures and gateway getter mocks were migrated in
12 existing pytest modules. They parse and pass diff checks; their execution
and full-suite compatibility await Ang's hosted CI. Canonical pytest is absent;
no package installation or fallback environment mutation was attempted.

## G17 — Discord channel policy (FC-17-E08/E09)

Pre-mutation contract: use the existing plugin YAML bridge rather than duplicate
it in core. For channel allow/deny only, preserve connected nonempty profile
snapshots first; otherwise explicit extra key presence, including empty lists
or strings, precedes scope-aware environment fallback. User/role/allow-all gate
precedence and actual outbound target/parent verification remain intact.

The real gateway loader proves E08's bridge already handles top-level Discord,
platforms.discord and gateway.platforms.discord settings, including empty lists,
without writing channel gates into process environment under scoped multiplex.
E08 is qualified retained behavior, not a newly implemented core bridge.
E09's pre-connect environment precedence failed four assertions before the fix;
all seven tests in `tests/gateway/test_v021_discord_channel_precedence.py` pass
afterward. They exercise real loader, adapter send denial, standalone policy,
connected snapshot/deny precedence, scoped misses and unrelated gate precedence.
External HTTP/Discord effects are fakes. Source commit and review binding are
recorded in the ledger and final review receipt.

## Verification and handoff

[V021_APPROVED_TESTS.json](V021_APPROVED_TESTS.json) holds actual commands/results;
[V021_APPROVED_REVIEW.md](V021_APPROVED_REVIEW.md) preserves the independent native
review, its initial finding and targeted correction. The cumulative human list
is only [V021_SAFE_SLICE_CHECKLIST.md](V021_SAFE_SLICE_CHECKLIST.md): every landed
restored subfeature maps exactly once, all results pending. The small stdlib test
`tests/test_v021_restoration_checklist.py` checks this relationship deterministically.
Automated evidence belongs here and in the ledger, not in a second human list.

The exact prior CI `3c8f7d0` / run 34523897060 remains a successful historical
checkpoint; it does not attest to this new candidate. No new hosted CI or staging
integration is claimed. Local validation uses clean subprocess environments,
bytecode disabled, read-only canonical interpreter, this tree's PYTHONPATH and
temporary HOME/HERMES_HOME/HERMES_BUNDLES_DIR inside the task evidence workspace.

## Remaining approved implementation

Continue from the ledger's 42 `approved_pending_implementation` subfeatures
plus G32's remaining thread-policy contract under FC-32-E01. The latter's
previous cleanup restoration remains valid; thread policy is not implemented. Next is
G17's nonvoice identity/ownership work: begin FC-17-E03/E04/E05/E06 by tracing
launch home, adapter durable paths and transport selection; write the chosen
per-path contract before mutation, retain the qualified channel policy and cron
registry, and test with disagreeing/missing profile identities. Then complete
G17 credential refresh, pairing, durable-owner recovery and served-profile
contracts; G19 notification destination policy; FC18 metadata/dashboard; FC20
broader environment scrub with durable child mutation refusal; G11 ancillary
cron validation; G14/G15 text/MEDIA/threading; G25 compression transaction
integrity; G26/G27 prompt cost/exact-model policy; G28 pip-compatible explicit
crypto specs; G30 publication/process classifier; G32 threads; G33 findings.
No additional Brian approval is required for these documented source slices.
Approval never implies a completed feature, permission for live effects, or
resurrection of excluded consumers. Exact pending subfeature IDs are in the
ledger and task-only approved-restoration-evidence.md.

Rollback boundaries: G17R's source and migrated tests form one coordinated unit;
reverting only scheduler keys would break sibling consumers. The channel policy
change and its tests form a separate unit. Documentation records approvals and
historical evidence and should not be treated as a production rollout.
