# FC-37: local Hindsight recorded evidence

Run this standalone CLI directly with Python 3.11+ on POSIX, from a trusted local
operator shell. It has no dependencies beyond the standard library and adds no
plugin registration, model tool, gateway seam, config setting, or activation step.

```bash
HERMES_HOME=/absolute/profile/home PYTHONPATH=/absolute/worktree \
  /absolute/python scripts/hindsight_history/cli.py \
  --profile-home /absolute/profile/home --session EXACT_SESSION_ID \
  --output /absolute/private-directory/new-report.json
```

`HERMES_HOME` must already identify the invoking profile; `--profile-home` is an
explicit matching assertion, not an authorization grant. No default profile,
sticky profile file, latest-session lookup, session-title lookup, parent-session
traversal, or config loading occurs. The local OS user must own the profile
(directory mode 0700) and database (0600, one hard link). Symlinks, unsafe
ancestors, root, and setuid invocations are refused. The requested existing
session record must belong to that profile: a populated `profile_name` must match
the standard Hermes directory name (`profiles/NAME`, otherwise `default`). Legacy
null/empty profile names use the owning database's profile boundary.

This is an **OS-owner CLI**, not a per-chat authorization mechanism. An OS account
that owns multiple profiles already has filesystem access to them. Do not expose
this command through gateway/slash handlers, remote requests, or a shared-account
service. The current `register_command` API supplies only `raw_args`; establishing
trusted chat identity there would need separately approved gateway work.

Output is JSON in a **new, exclusive 0600 file** inside an existing owner-only
directory. No evidence, paths, session IDs, arguments, response content, or
exception text go to stdout/stderr/logs. JSON escapes control characters. Existing
output paths are refused; a failed read may leave an empty private output file.
The operator is responsible for keeping this artifact private.

The report preserves literal Hindsight call arguments and tool results from only
the requested session's active or compacted rows, in insertion order. Rewound
rows are excluded. Compaction copies are preserved as separate physical records,
not presented as distinct historical actions. Calls/results carry message and
call IDs; missing or ambiguous results are labeled. Standalone named results can
survive missing call messages. Invalid call JSON is marked corrupt. Automatic
retain/recall activity is **unknown**, never inferred from today's settings. This
is persisted evidence, not proof a backend operation succeeded or a complete
original transcript. Ordinary chat and unrelated tool content are not exported.

An existing authorized session remains reportable when its message table is
missing, empty, or unreadable. Those statuses are distinct. A missing session,
unreadable database, unsafe file, or unsupported schema fails closed.

The reader takes a bounded (256 MiB) in-memory database snapshot, uses only SELECT
queries, and never opens the profile with SQLite's writable filesystem machinery.
It refuses nonempty WAL/rollback journals, changing files, and lock conflicts
instead of silently omitting committed evidence or creating sidecars. Use an
already checkpointed/offline database; this command does not checkpoint, stop
services, repair, or create an export. Large/live databases with outstanding WAL
are an intentional limitation of this small read-only implementation.

## Fresh reconstruction disposition

`--reconstruct` records an explicit request but **refuses it**, returns exit 3,
and writes the authorized recorded report with the reason. No reconstructed
content is produced. The current public Hindsight provider `handle_tool_call`
uses configured bank/tags, not an enforced caller session. `initialize` can
materialize service configuration/start embedded services; it cannot be used by
this viewer. Session lineage tags alone do not establish profile/bank authority
or guarantee that returned memory units contain only that session's content.

A future supported reconstruction needs a trusted bank binding and a public API
that enforces that entire scope without initialization side effects. Its output
must be labeled **reconstructed from current memory NOT original transcript**.
There is no speculative adapter, duplicated HTTP protocol, hidden recall,
retain/import/delete operation, or fallback to unscoped current memory here.

Exit codes: 0 recorded report written (inspect its evidence statuses), 2 refusal
or unavailable evidence, 3 recorded report written but reconstruction refused.

## Validation

Focused CI tests live in `tests/cli/test_hindsight_history.py`. They execute the
actual CLI and dispatch against synthetic databases built from the current
`hermes_state_common.SCHEMA_SQL`, with authorization/query-order assertions,
provider/SDK import and mutation spies, private-output checks, failure injection,
and profile/session isolation. No real user data or memory requests are needed.

```bash
TMP="$PWD/../test-tmp" TMPDIR="$PWD/../test-tmp" HOME="$PWD/../test-home" \
  HERMES_PYTHON="$PWD/../test-venv/bin/python" PYTHONPATH="$PWD" \
  bash scripts/run_tests.sh tests/cli/test_hindsight_history.py -q
```

Behavior evidence inspected: `git show d0b90d532f347a40f7ddc78aa223492272c3ff99`.
That commit was not cherry-picked: its global latest-session selection, provider
initialization, silent fresh recalls, and inferred automatic activity are absent.

## FC-37 synthetic UAT (2026-09-06)

Executed with the verified canonical interpreter, read-only, and this worktree
on `PYTHONPATH` (pytest itself used the isolated task venv because canonical
pytest is unavailable):

```bash
HERMES_HOME=/home/brian/.hermes/kanban/boards/operations/workspaces/t_497a91d5/synthetic-uat/profiles/alpha \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
  /home/brian/.hermes/hermes-agent/.venv/bin/python \
  scripts/hindsight_history/cli.py \
  --profile-home /home/brian/.hermes/kanban/boards/operations/workspaces/t_497a91d5/synthetic-uat/profiles/alpha \
  --session requested \
  --output /home/brian/.hermes/kanban/boards/operations/workspaces/t_497a91d5/synthetic-uat/recorded.json
```

This exact invocation returned `private-report-written`, exit 0. The private
report contained two synthetic evidence events from `requested`, excluding a
newer session. Reuse requires a new output filename (no overwrite).

| Synthetic CLI scenario | Exit | Observed disposition |
| --- | --- | --- |
| Requested session, recorded calls/results | 0 | Recorded evidence; newer session excluded |
| Explicit `--reconstruct` | 3 | Recorded evidence retained; reconstruction refused; no query |
| Absent requested session | 2 | Content-free denial; no report content |
| Existing session, missing messages table | 0 | Session present, messages/evidence missing |
| Existing session, empty messages | 0 | Session present, messages/evidence empty |
| Existing session, unreadable message schema | 0 | Session present, messages/evidence corrupt |

The task-local evidence artifact is `../synthetic-uat/uat-evidence.json`.
FC-37 disposition: recorded-evidence CLI implemented and synthetic UAT passed;
fresh-memory reconstruction remains refused because scope enforcement is not
available through the current safe public provider surface. No live acceptance,
activation, service changes, gateway edits, or push is part of this disposition.

Final focused runner result: **28 passed, 0 failed** (one CI test file), including
real synthetic WAL refusal/checkpointed recovery and missing/ambiguous results.
The base `package-lock.json` was verified byte-identical to
`9ccb53e3d15730fcae88b086ea954dfc377574aa`.
