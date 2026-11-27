# Scheduling release CI prerequisites

## Ask
Deliver the reviewed native scheduling repair through current-main-preserving integration,
required exact-head hosted CI, and ordinary qualified release. This branch carries the
actual prerequisite CI/test corrections tracked by issue251 and owner card operations/t_3f7d7068;
PR261 remains the existing publication route. No live scheduling retry is part of this branch.

## Plan
- [x] Preserve published staging and current main in integration base
  `e28304a99f96d67676095d6dc4087cba31995332`.
- [x] Prepare owning fixes for shallow/dashboard fixture premises, Desktop app selection,
  explicit-completion legacy features, and PM-managed Git layouts.
- [x] Preserve all strict assertions; diagnose ARM64 suspension through query-only failure output.
- [ ] Independent exact-candidate review; hosted execution of focused regressions and all strict lanes.
- [ ] Resolve Desktop reconnect duplication and any demonstrated native failures from actual hosted evidence.
- [ ] Fresh green exact-head qualification, ordinary release and independent native acceptance.

## State
As of 2026-11-27, source-only checkpoints `8c24b4df1f81ea8dde42d5f75b1323c7c0b7e1cd`
and `628cc1edb83fdc45dd8f0c3e1653861014492b1b` preserve current main
`03d3d0f585c8920d8ef0d977680ee1c1c1d6cff0` and the eight unchanged reviewed scheduling blobs.

Prepared changes use existing seams: genuine depth-1 preclone; explicit dashboard Node/npm
fixture preparation; executable-based Desktop app selection excluding crashpad; shared first-PM
legacy-extra selection with existing declines/carried semantics; recorded/staged PM Git-core
lookup with successful probes. Windows's overall job budget is240min because the old180min
job canceled its final file before that file's existing60min cap elapsed. Worker count2,
retries0, per-file cap3600s, tests, crash manifest, cleanup and strict aggregate are unchanged.

Ruff, relevant ESLint, Python/TypeScript syntax and whitespace checks passed. Behavioral
Python execution is unavailable locally without violating the required runner's scratch/isolation
boundary; the attempted namespace preflight was denied and is not bypassed. Desktop focused
runs with existing dependencies stopped before collection on missing packages. No dependency
installation/rebuild was performed; no behavioral RED/GREEN is claimed.

Desktop reconnect still needs a correction: concurrent persisted full reply and shorter live row
can render twice. The live event identity and full committed tail must both survive lost completion
and late deltas. The retained regression specifies one visible anchored reply without advancing the
raw stream cursor. Existing lost-completion protections and the full transcript oracle remain.
ARM64 still retains its original stopped/no-child-execution/job/handle assertions; new diagnostics
must establish whether the native failure is reporting or containment. No production containment change.

## Next
Ang owns review and existing-PR publication for authoritative hosted execution. This is a
qualification/diagnostic candidate, not merge/release readiness. Consume actual failed-job logs,
correct only demonstrated remaining roots, re-review affected deltas and obtain new exact-head
strict green CI. No blind rerun, gate waiver or unrelated work. Required gate: ci.yaml,
release=true, exact integrated staging SHA, successful All required checks pass(v2).
Release and original retry acceptance remain owed to their existing owners.
