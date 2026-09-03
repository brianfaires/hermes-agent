# Feature disposition ledger — Hermes v0.21.0 reconstruction

Every local behavior must pass both gates:

1. **Need gate:** Is the behavior still useful enough to justify carrying and operating it?
2. **Placement gate:** Can it live outside upstream core?

Preferred placement order:

1. Existing upstream configuration
2. Skill or operational runbook
3. Standalone profile plugin under `~/.hermes/plugins`
4. In-tree plugin or platform adapter
5. Existing service-gated tool or hook
6. Small generic upstream seam plus external implementation
7. Direct upstream-core patch only when no safer boundary exists

Allowed decisions: `KEEP`, `REWRITE`, `DROP_UPSTREAM`, `DROP_LOW_VALUE`, `DROP_OUT_OF_SCOPE`, `DEFER_HUMAN_VALUE`, and `DEFER_REPRODUCTION`.

For each feature record: legacy commits/branches, current symptom, upstream evidence, value judgment, chosen placement, migration commit, tests, review, rollback tag, and UAT requirement.

No historical commit is replayed merely because it exists. Mixed commits are split by behavior; historical dependency pins are regenerated from the v0.21 dependency graph.

| Feature | Legacy source | Current symptom | Decision | Placement | Migration commit | Tests | Rollback |
|---|---|---|---|---|---|---|---|
| Run hosted CI on persistent staging pushes before main promotion | `597d7030585eb574e30b40eb44c11764f79f7891` | Current `.github/workflows/ci.yaml` only runs push orchestration on `main`, so staging pushes do not receive the complete hosted gate. | `KEEP` | Existing upstream CI orchestrator trigger | This slice | `tests/ci/test_staging_workflow_gate.py` | Revert this slice to remove the staging push trigger. |
