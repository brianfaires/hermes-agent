# Scheduling release CI prerequisite handoff

2026-11-27 — operations/t_3f7d7068, issue251, PR261. Round-two source preparation;
no commit, review, publication, hosted dispatch, or release.

Base and unchanged HEAD: `e28304a99f96d67676095d6dc4087cba31995332`.
Branch: `ang/scheduling-release-ci/t_3f7d7068`.
Merge-base with local main: `03d3d0f585c8920d8ef0d977680ee1c1c1d6cff0`.
Historical failed hosted execution used `bfe50ca6fec28e46734047b119dfbdf61c8ebc10`;
it does not qualify this patch.

Prepared fixes:

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

Validation: changed Python files parse; changed TypeScript passes syntax transpilation
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
- Windows cancellation: check-run annotations confirm the 180-minute job timeout. The
  unfinished file runs two serial old-gateway upgrades; post-cancellation descendants
  belong to the standard-user source-completion path. A 225-minute job budget is proposed
  for Ang's decision, retaining two workers, the 3600-second file cap, all cases/assertions
  and strict gates. No workflow edit.
- Windows Git locator, ARM64 containment diagnosis, and PM SDK migration need the next
  bounded native/evidence slice. No production containment changes.

Full matrix: `evidence/ci-prerequisites/ROOT-MATRIX-ROUND2.md`.
Patch, exact commands, static output and isolation facts: `evidence/ci-prerequisites/round2/`.
Original reports and worker notes remain. Worktree dependency symlinks are listed for parent
verification. Launcher target/hash match the brief; no service-state inference from the
sandbox PID view. All new temporary/cache artifacts are worktree-local.

Ang checkpoints using the established Brian-author/ang-ineering-committer identity; no
configuration changed. Independent review and actual hosted qualification remain mandatory.
Release gate unchanged: `ci.yaml`, `release=true`, exact integrated staging HEAD, successful
`All required checks pass (v2)`. No release on unexecuted tests.
