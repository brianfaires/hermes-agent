# Runner scratch repair — t_1c2e7633

As of 2026-11-18.

## Ask

Allow the canonical runner to use writable task storage in a workspace-write
sandbox. Ang owns design, acceptance, Git and board actions. This slice implements
only the approved optional `HERMES_TEST_SCRATCH_ROOT` infrastructure knob.

## Plan

Use the absolute directory supplied by `HERMES_TEST_SCRATCH_ROOT` as the base for
the existing per-user child. Keep cleanup scoped to that child. Preserve unset
and empty defaults, isolation and retries. Forward the knob through the shell's
explicit allowlist. Usage and path constraints are in `tests/AGENTS.md`.

Documentation consulted: root and tests `AGENTS.md`, `CONTRIBUTING.md` test
environment guidance, and Ang's
`hermes-fork-dev/references/repository-worktree-runtime-isolation-and-global-launcher-safety.md`.
The supported external `HERMES_PYTHON` branch supplies dependencies without
activation, but cannot select runner storage: `_runner_scratch_root` selects
`/var/tmp` independently of `TMPDIR`, and the shell strips unlisted knobs.
An extension/plugin cannot configure this pre-test runner decision. The narrow
runner change is therefore necessary; no application runtime change is involved.

## State

Coverage includes mocked default selection, real override creation and child-only
sweep, and a real shell/pytest canary. The original two-UID namespace assertions
are retained; their only change is clearing the inherited override. Mocking
`getuid` here tests per-user namespace selection, not host OS identity.

The added mocked-`makedirs` test checks that the selected root's parent equals
the supplied base. An external test imports the complete base runner from
`e55db73863a4a3c866ed5ebdd17b0ca97dca0617`, binds it to that new test, and invokes
only that test under the fixed canonical wrapper: 1 collected, 1 assertion
failure, exit 1. All directory creation in the base function is mocked; the old
outer runner is never executed. No production runner file changed for this proof.

Original GREEN was 7 tests; the added test and restored two-UID coverage require
fresh evidence. Correction commands, source hashes, raw logs and exits are in the
parent task workspace's `correction-evidence.md`, additional to the preserved
`implementation-evidence.md` and original logs.
Fresh canonical focused run: 8 passed, 0 failed across 2 files, exit 0, 98.5 seconds
runner wall time. Shell syntax, installed Ruff and diff whitespace checks passed.

The unchanged restart overlay collected 12 cases but timed out after 300 seconds
without assertion results (exit 1). The collection-only prerequisite is proven;
`/restart` behavioral qualification remains outstanding.
The unchanged retention overlay collected 1 case and produced assertion RED:
the scheduled execution was missing after 1,001 unrelated completions (exit 1,
including the runner's normal retry). Neither draft was altered.

This workspace has long, hidden ancestors. Qualification covers only the focused
paths; AF_UNIX, hidden-directory search tests and the full suite remain CI work.

## Next

Ang independently reviews the implementation and qualification evidence, removes
the two unchanged qualification overlays after freeze, and owns check-in.
Resolve `/restart` behavioral qualification separately. This evidence makes no
feature acceptance or source integration claim.
