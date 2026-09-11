# Finite compatibility preparation tail

Continues accepted `eda8d0537b1b66000b5ade38855343634ddd4e75`.
Ang reports that exact candidate was independently inspected, its seven new
tests rerun, and fast-forwarded/pushed to source-only staging. **Hosted CI is
pending** until Ang supplies its actual result. Prior successful `3c8f7d0`
receipts remain historical evidence, not CI evidence for this tail.

This follow-up changes documentation/evidence only. No production/test source,
manifest/lock, workflow, guard, venv, live state or remote ref changed. The
accepted [continuation review](CONTINUATION_REVIEW.md) is preserved verbatim;
no second broad review or repeat of accepted tests was requested or performed.

## G28: actual wheels-only resolution matrix

Read specs from current `pyproject.toml:202,400-404` and
`tools/lazy_deps.py:212-219`, plus exact old main
`2945588a014543d47c9e5e4a0d92ba6e361387c1:pyproject.toml:164` and
`tools/lazy_deps.py:162-166`. The proposed versions are historical values,
not guessed upgrades. These probes isolate the voice dependency slice and its
transitive dependencies; they do not resolve the complete Hermes project.

Host: Linux x86_64, CPython 3.11.15. Existing pip 24.0 and uv 0.11.8 were used
with PyPI, clean subprocess environments, fresh temporary homes and caches.
Pip used `--isolated install --dry-run --ignore-installed --only-binary=:all:
--no-build-isolation --report`; uv used `--no-config pip compile --python
<canonical-python> --only-binary=:all:`. No package installation, Python
download, bootstrap, third-party source build or runtime import was performed.

| Requirements | pip result | uv result |
| --- | --- | --- |
| Baseline `discord.py[voice]==2.7.1`, no override | Resolves: PyNaCl 1.5.0, davey 0.1.6 | Same relevant versions |
| Naive raised floor: baseline plus historical `PyNaCl==1.6.2` | Exit 1, ResolutionImpossible: voice extra requires `PyNaCl>=1.5.0,<1.6` | Exit 1, same unsatisfiable intersection |
| Proposed explicit `discord.py==2.7.1`, `PyNaCl==1.6.2`, `davey==0.1.6` | Resolves all three exact pins | Resolves all three exact pins |
| Baseline plus shipped uv override `pynacl>=1.6,<1.7` | Not a pip feature; not run as a pip arm | Resolves PyNaCl 1.6.2, davey 0.1.6 |

All successful arms selected aiohttp 3.14.3 transitively. Raw commands, output,
resolver errors and complete selected versions are in
[V021_COMPATIBILITY_TAIL_RECEIPTS.json](V021_COMPATIBILITY_TAIL_RECEIPTS.json).
Downloaded wheel/metadata cache contents were disposable; no live or fallback
venv was written. Resolver success is not runtime compatibility, a complete
project lock qualification, a security attestation, or cross-platform proof.

**Decision-ready recommendation:** approve a separate G28 slice replacing the
voice extra with the three historical explicit pins in manifest and lazy deps,
retaining all unrelated current dependency versions. Regenerate/validate the
lock and test applicable supported runtime paths as part of that approved
implementation. A naive extra-plus-floor addition is concretely unsatisfiable;
uv's override does not make the pip interface equivalent. The resolution
prerequisite is now completed without waiting on a packaging decision. G28's
Critical classification is unchanged, and no parked voice source was used.

## G30: pure classification matrix, never command execution

Executed unmodified nested pure classifier bodies and literal constants from
old/current `tests/conftest.py` using AST extraction, as explicitly permitted
by the tail brief. No pytest fixture, matrix command, signal, service operation
or actual process-ancestry walk ran. Six ancestry rows use fixed synthetic
parent data at the psutil boundary. Source hashes and exact extracted line
ranges are recorded in the durable receipt.

| Input / proposed contract | Old | Current |
| --- | --- | --- |
| Direct `pkill -f hermes`: block | Block | Block |
| `command` wrapper, argv representation: block | Block | Allow — proposed contract fails |
| `command pkill -f hermes`, shell string: block | Block | Block |
| `env MODE=test pkill -f hermes`: block | Block | Block |
| `bash -c 'pkill -f hermes'`: block | Block | Block |
| Bash sequence with later killer: block | Block | Block |
| `echo pkill hermes`: allow | Allow | Allow |
| `env echo pkill hermes`: allow | Allow | Block — proposed contract fails |
| `sudo -u fixture pkill -f hermes`: block | Block | Block |
| `timeout 2 pkill -f hermes`: block | Allow — proposed contract fails | Block |
| `env -u MODE pkill -f hermes`: block | Allow — proposed contract fails | Block |
| `pkill --full python`: block | Block | Allow — proposed contract fails |
| systemctl gateway restart / status | Block / allow | Block / allow |
| Synthetic self, initial child, new descendant | In subtree | In subtree |
| Synthetic ancestor, foreign process, broadcast PID -1 | Outside subtree | Outside subtree |

The `command` argv row tests representation handling only: `command` is a shell
builtin, and ordinary subprocess argv does not itself imply a shell executes
it. Do not promote that result into a demonstrated host exploit. The shell
string arm is separately recorded. Other wrapper rows are likewise data;
not a single proposed process-killing command was executed.

**Decision-ready recommendation:** approve a separately tested parser change
that recognizes executable positions and value-taking wrapper options, handles
shell command segments, preserves current timeout/env-unset refusals, and
recognizes the full-pattern option without classifying an innocent argument as
an executable. Preserve the ancestor/foreign refusal. Blind restoration of the
old parser fails the timeout and env-unset rows. All failed proposed contracts
remain explicit. G30 stays Critical; SARIF publication is a separate policy
choice and no workflow publication behavior is inferred from this matrix.

## Remaining decisions versus safe prerequisites

The finite G28 resolution and G30 classifier prerequisites are complete. No
remaining **known safe prerequisite qualification** from this bounded brief is
outstanding. That does not declare whole-scope readiness or erase Critical
implementation gates. Additional acceptance tests for a future chosen contract
belong to its approved implementation, not an indefinite pre-approval audit.

- G17: retain distinct transport principal, conversation profile and durable
  owner; resolve disagreement explicitly without cross-profile credential
  fallback. Existing anchors in [critical-gates.md](critical-gates.md) and the
  prior scope/registry evidence suffice to present that decision. G17R must
  capture profile-qualified keys through registration/release/stale sweep while
  retaining global drain visibility and profile-local manual-run checks.
- G25: never delete or reopen across a published canonical continuation with
  concurrent child writes/leases. Recommend placing necessary tail persistence
  inside the pre-publication transaction, or explicit reconciliation that keeps
  the child authoritative on later failure. The chosen transaction contract
  needs implementation tests; no additional broad identity/runtime audit is a
  prerequisite to requesting the decision.
- Other existing gates retain their concrete recommendations in the prior
  [qualification document](V021_CONTINUATION_QUALIFICATION.md) and decision
  register. Historical compatibility choices, not missing engineering planning,
  are the remaining constraints. Human UAT remains deferred and cumulative.

External task evidence lives under
`/home/brian/.hermes/kanban/boards/engineering/workspaces/t_4a31a31b/`:
`compatibility-tail-evidence.md`, `compatibility-tail-result.md`, the two
`compatibility-tail-*.py` probes and `compatibility-tail-artifacts/`.
These are task-only provenance, not GitHub-relative document links.
