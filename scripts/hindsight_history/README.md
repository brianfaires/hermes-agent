# FC-37: local Hindsight evidence and recovery

Run this standalone CLI with Python 3.11+ on POSIX, from a trusted local operator
shell. Recorded viewing uses the standard library. Explicit reconstruction also
uses the already-installed public `hindsight-client` SDK and Hermes' public
`load_env_file` parser for selected-profile credentials. Nothing is installed or activated.

```bash
HERMES_HOME=/absolute/profile/home PYTHONPATH=/absolute/worktree \
  /absolute/python scripts/hindsight_history/cli.py \
  --profile-home /absolute/profile/home --session EXACT_SESSION_ID \
  --output /absolute/private-directory/new-report.json
```

Add `--reconstruct` only to request one fresh, scoped recall from current memory.

## Authorization and private output

`HERMES_HOME` must already identify the invoking profile; `--profile-home` is an
explicit matching assertion, not an authorization grant. No default profile,
sticky profile file, latest-session lookup, title lookup, or parent traversal
occurs. The local OS user must own the profile (0700) and database/sidecars
(0600 or stricter, one hard link). Symlinks, unsafe ancestors, root, and setuid
invocations are refused. The explicit session record must exist. A populated
`profile_name` must match the standard Hermes directory name (`profiles/NAME`,
otherwise `default`); legacy null/empty names use the owning DB's profile boundary.

This is an **OS-owner CLI**, not per-chat authorization. Run it as a separate
process; do not embed it in a process with writable SQLite connections or expose
it through gateway/slash handlers, remote requests, or shared-account services.

Output is JSON in a **new, exclusive 0600 file** inside an existing owner-only
directory. No evidence, paths, session IDs, arguments, response content, or
exception text go to stdout/stderr/logs. Optional SDK diagnostics are suppressed.
JSON escapes control characters. Existing output paths are refused; a failed
source read may leave an empty private output file. Keep the artifact private.

## Recorded evidence, including live WAL

Only the requested session's active or compacted rows are queried. Rewound rows,
ordinary chat, and unrelated tool content are excluded. Literal Hindsight call
arguments/results retain message and call IDs. Missing, empty, corrupt, recorded,
and ambiguous results remain distinguishable. Compaction copies are separate
physical records, not proof of distinct historical actions. Automatic memory
activity is **unknown: not inferred**, regardless of current configuration.

An authorized session remains reportable when its message table is missing,
empty, or unreadable. Missing sessions and unreadable databases fail closed.

SQLite opens the source using `mode=ro&readonly_shm=1&vfs=unix&cache=private`, with
`query_only` enabled, trusted schema disabled, and one read transaction. Committed
live WAL evidence is visible. The Unix VFS's `readonly_shm` option prevents SHM
read-mark writes and creation; SQLite owns journal validation and locking. There
is no database copy, header rewrite, manual journal parser, checkpoint, or repair.
See [SQLite WAL support](https://www.sqlite.org/wal.html) and the
[Unix VFS implementation](https://github.com/sqlite/sqlite/blob/master/src/os_unix.c).

SQLite 3.22+ with the Unix VFS is required. Nonempty rollback journals, unsafe
sidecars, a nonempty WAL without existing SHM, and states SQLite cannot read
without recovery are refused. A stopped WAL database without its SHM may therefore
be unavailable even after checkpointing. There is no unsafe fallback.

## Explicit current-memory reconstruction

After source/session authorization, `--reconstruct` reads only the selected
profile's `hindsight/config.json`, plus its `.env` for `HINDSIGHT_API_KEY` if the
JSON has no key. Both must meet the private-file boundary. Environment-variable
interpolation is disabled; shared legacy config, ambient credentials, config
migration, provider imports, and `provider.initialize()` are never used. The
default recorded viewer reads none of these files and imports no SDK.

A configured `bank_id_template` must contain exactly one plain `{profile}` and
otherwise only literal ASCII letters, digits, dashes, or underscores. Its profile
name and rendered bank must survive the provider's normalization unchanged.
Static/shared banks, missing/unknown placeholders, format conversions, and
sanitization ambiguity fail closed **before SDK construction**. Static `bank_id`
is never a fallback. For example, `hermes-{profile}` binds profile `alpha` to
`hermes-alpha`. Cloud and existing external services are supported; embedded
service management is unavailable. Existing provider URL defaults apply. Cloud
requires a selected-profile key; external services may be unauthenticated.

One public `Hindsight.recall` requests:

- That profile's validated bank and the exact `session:EXACT_SESSION_ID` tag.
- `tags_match="all_strict"` (untagged results excluded).
- Only `world` and `experience` facts, with entities, chunks, source facts, and
  trace disabled; no reflection or synthesized observations.

Before **any** fresh content is emitted, every returned fact must have exactly
that session among its session tags, provider metadata `session_id` matching the
request and `agent_identity` matching the profile, an allowed fact type, and no
synthesized source-fact lineage. Wrong/missing provenance or unsolicited expanded
response sections reject the whole fresh response. Only validated fact IDs,
types, text, and the verified scope are exported; arbitrary metadata/context is
excluded. Missing/empty/corrupt local messages do not prevent the single recall.

Every fresh fact and its containing section are labeled:
**reconstructed from current memory NOT original transcript**. This is a bounded
current recall, not a complete historical reconstruction or evidence of past
automatic activity. No retain, import, delete, bank setup, service start,
auto-install, or unscoped fallback is performed. SDK signature incompatibility,
missing config/SDK, and backend failures yield content-free errors and no fresh
facts. An empty successful recall is distinct from unavailability.

Exit codes: **0** report written (inspect statuses), **2** source/authorization
failure, **3** recorded report written but fresh reconstruction unavailable.

## Focused synthetic evidence

`tests/cli/test_hindsight_history.py` imports the actual current
`hermes_state_common.SCHEMA_SQL`. Tests execute the subprocess CLI with committed
live WAL evidence that an immutable DB-only read cannot see, and compare DB/WAL/
SHM bytes, modification times, permissions, and directory entries before/after.
They cover scope authorization before message/config reads, bank and provenance
isolation, missing-message recovery, private output, redaction, and mutation spies.

Installed SDK compatibility was inspected read-only: `hindsight-client 0.6.1`.
Its constructor is `(base_url, api_key=None, timeout=300.0, user_agent=None)`.
`recall(self, bank_id, query, types=None, ..., tags=None, tags_match='any',
tag_groups=None)` also exposes `include_entities`, `include_chunks`,
`include_source_facts`, `trace`, `max_tokens`, and `budget`. `all_strict` excludes
untagged facts. `RecallResult` exposes `type`, `tags`, and `metadata`. Repository
`sync_turn()` attaches `session:<id>`; `_build_metadata()` records `session_id`
and `agent_identity`; `_resolve_bank_id_template()` supports `{profile}`.

The optional installed-SDK tests bind these real signatures and run the actual
SDK/CLI through a synthetic local HTTP server, asserting exactly one scoped
recall request, no mutations, positive recovery from a missing message table,
backend-error redaction, and rejection of wrong-session responses. Set
`FC37_SDK_PYTHON` to an existing interpreter with the SDK; otherwise these optional
cases use the test interpreter and skip if the SDK is absent. No live profile
config, history, or memory is used.

The runner clears custom environment variables. This workspace-only wrapper
restores synthetic test paths and the read-only SDK interpreter selection:

```bash
cat > ../fc37-test-python <<'SH'
#!/bin/sh
export FC37_SDK_PYTHON=/home/brian/.hermes/hermes-agent/.venv/bin/python
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR=/home/brian/.hermes/kanban/boards/operations/workspaces/t_497a91d5/test-tmp
export HERMES_HOME=/home/brian/.hermes/kanban/boards/operations/workspaces/t_497a91d5/test-home
exec /home/brian/.hermes/kanban/boards/operations/workspaces/t_497a91d5/test-venv/bin/python "$@"
SH
chmod 700 ../fc37-test-python
TMP="$PWD/../test-tmp" TMPDIR="$PWD/../test-tmp" HOME="$PWD/../test-home" \
  HERMES_PYTHON="$PWD/../fc37-test-python" \
  bash scripts/run_tests.sh tests/cli/test_hindsight_history.py -q --file-retries 0
```

Final output (exit 0; all three installed-SDK cases ran, no skips):

```text
[100.0% |    24/~24 | ✓82 | ✗ 0] ✓ tests/cli/test_hindsight_history.py (82✓, 91.4s)
=== Summary: 1 files, 82 tests passed, 0 failed (100% complete) in 91.4s (16 workers) ===
```

`git diff --check` also returned exit 0 with no output. Validation used only
synthetic config, history, and memory; no deployment, activation, canonical
writes, push, card creation, Claude call, or independent review was performed.

Read-only signature evidence command:

```bash
HOME="$PWD/../test-home" HERMES_HOME="$PWD/../test-home" PYTHONDONTWRITEBYTECODE=1 \
  /home/brian/.hermes/hermes-agent/.venv/bin/python - <<'PY'
import inspect, sqlite3
from importlib.metadata import version
from hindsight_client import Hindsight
print('hindsight-client', version('hindsight-client'))
print('SQLite', sqlite3.sqlite_version)
print('Hindsight', inspect.signature(Hindsight))
print('recall', inspect.signature(Hindsight.recall))
PY
```

Output (exit 0):

```text
hindsight-client 0.6.1
SQLite 3.50.4
Hindsight (base_url: str, api_key: str | None = None, timeout: float = 300.0, user_agent: str | None = None)
recall (self, bank_id: str, query: str, types: list[str] | None = None, max_tokens: int = 4096, budget: str = 'mid', trace: bool = False, query_timestamp: str | None = None, include_entities: bool = False, max_entity_tokens: int = 500, include_chunks: bool = False, max_chunk_tokens: int = 8192, include_source_facts: bool = False, max_source_facts_tokens: int = 4096, tags: list[str] | None = None, tags_match: Literal['any', 'all', 'any_strict', 'all_strict'] = 'any', tag_groups: list[dict[str, typing.Any]] | None = None) -> hindsight_client_api.models.recall_response.RecallResponse
```

The predecessor is `8706b00a46e3a449eb26a5c9ba5f758f7b4da470`; its recorded-evidence
and authorization work is preserved. Historical implementation intent was
inspected at `d0b90d532f347a40f7ddc78aa223492272c3ff99`. That commit was not
cherry-picked: its global latest-session selection, provider initialization,
silent recalls, and inferred automatic activity remain excluded.
