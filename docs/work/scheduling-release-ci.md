# Scheduling release CI prerequisite handoff

2026-11-27 — operations/t_3f7d7068, issue251, PR261. Round-three source preparation;
no commit, review, publication, hosted dispatch, or release.

Original base: `e28304a99f96d67676095d6dc4087cba31995332`.
Round-three base and unchanged HEAD: `8c24b4df1f81ea8dde42d5f75b1323c7c0b7e1cd`
(Ang's checkpoint preserving round two).
Branch: `ang/scheduling-release-ci/t_3f7d7068`.
Merge-base with local main: `03d3d0f585c8920d8ef0d977680ee1c1c1d6cff0`.
Historical failed hosted execution used `bfe50ca6fec28e46734047b119dfbdf61c8ebc10`;
it does not qualify this patch.

Preserved round-two fixes:

- Shallow upgrade scenarios seed depth-1, single-branch checkouts through the existing
  preclone seam, then run the unchanged predecessor installer. Both depth1 and
  passive-check fixtures use this shape. Baseline, fetch-byte bound, upgrade assertions
  and strict failure policy remain. Passive checks also assert the initial shallow
  premise. Fixture docs no longer claim the predecessor creates shallow clones.
- Handoff explicitly provisions managed Node/npm inside its hosted disposable installation
  using the predecessor-supported `hermes pm install` before starting the fleet.
  Dashboard health/handoff assertions and disabled lazy-install policy remain.
  No provisioning was executed locally.
- Desktop's update harness selects the packaged Hermes executable inside the release
  directory, excluding detached crashpad candidates. A real-process harness regression
  includes the app, its renderer, and a detached crashpad-shaped executable. Actual app
  termination and surviving-gateway E2E assertions remain intact. Source proves the
  selector defect; the historical selected PID was not logged, so resolution of that
  hosted app-update failure remains unproven.

Round-two validation: changed Python files parse; changed TypeScript passes syntax transpilation
and ESLint; `git diff --check` passes. No behavioral test passed locally. Python RED/GREEN
remains unavailable: the mandatory wrapper's external scratch root and the parent's denied
namespace route cannot meet this phase's isolation boundary. Neither route was retried
or bypassed. Existing root/web Node dependencies match package/lock identities, but Desktop
Vitest cannot initialize without driver.js; the harness also lacks @playwright/test.
No dependencies were installed.

Deferred:

- Reconnect duplication: the overlay intentionally retains a committed reply alongside
  a shorter live row to avoid losing its tail. Exact hosted ordering and safe late-delta/
  completion reconciliation remain unproved. No speculative change or oracle weakening.
- Windows/PM hosted execution remains pending for the round-three corrections below.

Full matrix: `evidence/ci-prerequisites/ROOT-MATRIX-ROUND2.md`.
Patch, exact commands, static output and isolation facts: `evidence/ci-prerequisites/round2/`.
Original reports and worker notes remain. Worktree dependency symlinks are listed for parent
verification. Launcher target/hash match the brief; no service-state inference from the
sandbox PID view. All new temporary/cache artifacts are worktree-local.

Ang checkpoints using the established Brian-author/ang-ineering-committer identity; no
configuration changed. Independent review and actual hosted qualification remain mandatory.
Release gate unchanged: `ci.yaml`, `release=true`, exact integrated staging HEAD, successful
`All required checks pass (v2)`. No release on unexecuted tests.

Round-three bounded corrections:

- Explicit update completion now uses the same first-selection helper as launch recovery:
  inventory legacy SDKs only before PM facts exist, carry them with existing declined-extra
  semantics, and leave recorded selections to PM. No data format, credential, or policy change.
  Public completion-entrypoint regressions observe the real PM client's first worker request;
  the heavy hosted legacy migration fixture and all SDK imports remain unchanged.
- Git-core lookup adds recorded and lock-pinned staged PM entries for clangarm64, mingw64,
  and mingw32. Existing Program Files/legacy candidates remain. A nonzero version probe
  without the guard message now counts as unavailable, not healthy. Retained layout tests
  cover all three directories and both PM sources; native command-substitution tests remain.
- Only the Windows install/update workflow's job budget changes: 180 to Ang's selected
  240 minutes. The observed final file had ~31 minutes left of its 60-minute cap;
  ~209 minutes plus setup/cleanup margin motivates 240. Two workers, zero retries,
  file cap, crash manifest, cleanup and strict aggregate remain. No speedup claim.
- The failed-setup test prints child PID/status/marker and a documented WinAPI thread
  snapshot's PID/TID/suspend counts on stopped-status failure. Capture is query-only on
  the spawned child's handle, without context/memory capture or suspend/resume calls.
  Snapshot resources are freed. All original assertions and production containment remain.

Round-three validation: all eight edited/new Python files pass AST parsing and Ruff;
`git diff --check` passes. Behavioral RED/GREEN is unavailable locally because the canonical
runner's scratch/namespace limitation remains; no denied route was retried. Native snapshot
layout/results, PM SDK migration, Git substitution and the 240-minute hosted schedule remain
unexecuted. Launcher hash unchanged. Desktop/reconnect and original scheduling files untouched.
Affected old review is invalidated; independent exact-head review and strict hosted execution
remain Ang's next steps. Exact commands, results, source evidence, patch and acceptance gaps:
`evidence/ci-prerequisites/ROUND3.md` and `evidence/ci-prerequisites/round3/`.
