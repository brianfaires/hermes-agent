# Approved restoration batch 2

Qualified cohesive source batch continuing accepted `4cef65cada7ac1e483d117949862784beb8dc7aa`. Source candidate
`59b7ab83634cdf1e57c091e9072002b18b7eba3b`, tree `8379ce7036b86bb76c284d3a4a1dda8a01672207`. Documentation-bound final HEAD/tree are recorded
in `../approved-batch2-evidence.md` outside source. Whole initiative is unfinished.
Brian's existing source/tests/staging approval remains effective; Ang owns staging
integration and exact-SHA hosted CI. Critical classifications remain unchanged.
Ang reported batch 1 origin/staging at `4cef65cada7ac1e483d117949862784beb8dc7aa` and CI 34545497788 queued;
this is a reported historical checkpoint, not a current CI result.

## Implemented and qualified

| Commit | IDs | Result |
| --- | --- | --- |
| 43407c957e86052cb08995bc97dd55f294a5a3ec | FC-17-E25/E26/E30 | Durable owner checks and atomic session resume, including failure paths. |
| 38a634aee34952f3e66d7b2bceba95b438650526 | FC-17-E14/E15/E16 | Fresh private profile credentials and scoped child environment, without process dotenv mutation. |
| 38a634aee34952f3e66d7b2bceba95b438650526 | FC-17-E13 | Existing shared bounded executor qualified; historical per-home pool/fairness is not restored. |
| 59b7ab83634cdf1e57c091e9072002b18b7eba3b | FC-17-E03/E04/E05/E06/E20/E21/E22/E23/E24 | Pinned launch/adapter homes, transport ownership, pairing and all six delayed nonvoice control views. |

No archive `profile_owner` schema introduced: durable ownership uses current
`sessions.profile_name` plus legacy key fallback. Resume checks target and affected
compression ancestors inside BEGIN IMMEDIATE and publishes memory only after DB
commit. Existing reset-child stabilization, full peer metadata, branch/delegate/tool
lineage exclusions, generation ordering and leases remain. No live migration ran.

Runtime home is distinct from receiving transport home. Missing explicit runtime
refuses; stale transport never selects another routed bot. Adapter state, grants,
DM policy and delayed controls stay with the receiving bot. Agent cache identity
includes resolved home while retaining existing dimensions and stable same-home
values; no new core tool or mid-conversation prompt/toolset mutation.

Credential refresh returns the locked per-home snapshot directly. Scope tokens
reset even on setup failure; scoped dotenv imports avoid process writes. Cron
scripts resolve scope before the existing sanitizer, preserving provider-secret
scrub and child mutation refusal. Single-profile legacy shell fallback remains;
multiplex without scope fails closed. Named bot receiver resolves its own secrets.

## Validation and independent review

[Native review](V021_APPROVED_BATCH2_REVIEW.md) is reproduced verbatim. It inspected
all 12 changed production/test files at exact source candidate and verified their
SHA256s against committed blobs. No concrete P0/P1 blocker found. Documentation
changes are outside that source review binding.

**36 new tests passed independently:** ownership 12, private credentials 9,
transport identity 15. Parent supplemental runs passed prior G17R 8, channel policy
7 and scope 2. Coverage validator 1 passed: **54 unique focused tests**, with
repeated runs counted only once. Commands/stdout/returncodes and hashes are in
[V021_APPROVED_BATCH2_TESTS.json](V021_APPROVED_BATCH2_TESTS.json).

Commands (runner uses read-only canonical Python, per-file sanitized environment,
fresh task-local HOME/HERMES_HOME/HERMES_BUNDLES_DIR/TMPDIR, source PYTHONPATH,
bytecode disabled):

```
python3 ../run-approved-focused.py ../batch2-review-ownership-credentials-tests.json tests/gateway/test_v021_durable_ownership.py tests/cron/test_v021_private_credentials.py
python3 ../run-approved-focused.py ../batch2-review-transport-tests.json tests/gateway/test_v021_transport_identity.py
python3 ../run-approved-focused.py ../batch2-checklist-tests.json tests/test_v021_restoration_checklist.py
```

Tests use real temporary SQLite/filesystem/config imports, harmless joined script
children, pairing stores and control entrypoints with fake external boundaries.
Known safe SQLite DELETE-journal fallback warning was observed; no repair.
Named bot test mocks profile resolution/spawn and proves the passed destination
environment only: **mounted-root child CLI startup is not E2E qualified**.
No live providers, platform delivery, restart, full local suite, installs or human
UAT. Canonical pytest unavailable; exact candidate hosted CI remains outstanding.
Read-only existing ownership fixture compatibility pass found no edits needed;
that inspection is not a pytest pass. Changed Python parses; diff checks pass.

Ledger preserves 51 exact audit IDs (37 old_main / 14 archive_only), 239 original
evidence entries and 241 subfeatures; historical field values and ID order compared
to accepted batch 1 unchanged. Prior source review/test/continuation receipts are
unchanged. [Cumulative checklist](V021_SAFE_SLICE_CHECKLIST.md) covers 52 landed IDs
once across 22 grouped rows, all human results pending. E13 is qualified retained,
so it is not falsely counted as an additional landed restoration.

## Exact remaining continuation

Next: FC-17-E17/E18/E29, served-profile publication/runtime ownership, CLI readers
and successful-profile bookkeeping. Write the compatible contract before source
mutation; preserve current lifecycle/drain and transport isolation. Then remaining
feature gates below. No further Brian approval is needed within existing scope.

**26 approved pending ledger subfeatures:**

FC-10-E02, FC-11-E08, FC-14-E01, FC-14-E02, FC-15-E01, FC-17-E17, FC-17-E18, FC-17-E29, FC-18-E03, FC-18-E04, FC-18-E05, FC-19-E01, FC-19-E02, FC-19-E03, FC-19-E04, FC-19-E05, FC-20-E12, FC-25-E04, FC-26-default, FC-27-E01, FC-27-E02, FC-28-E02, FC-30-E01, FC-30-E03, FC-30-E04, FC-33-E01.

**Additional pending contract:** G32 thread ceilings under FC-32-E01 (no extra ID).
These include webhook namespace, cron ancillary validation, Discord literal text/
standalone MEDIA/free threading, Kanban metadata/dashboard/notifications, wider
child environment scrub, compression publication rollback, prompt descriptions/
model policy, packaging, OSV publication/process classification and Tirith findings.
Voice, retired consumers and archive-only features remain excluded.

All implementation/review workers and test subprocesses completed; no child remains
active. Source worktree and task evidence only were written. No push, staging/main
or canonical changes, live configuration/credentials/data/venv changes, activation,
install/setup/doctor, real process-killer, paid service or Kanban operation.
Canonical launcher readback remains `/home/brian/.hermes/hermes-agent/.venv/bin/hermes`.

## Pre-mutation contracts (preserved)


# Batch 2 ownership contract (before source mutation)

FC-17-E25/E26/E30 restore durable recovery and explicit resume ownership using the
current schema's `sessions.profile_name`, never archive-only `profile_owner`.
A nonempty durable profile stamp is authoritative even when a row's route key
matches the request. Legacy NULL stamps fall back to the existing gateway-key
namespace. In multiplex mode the requested namespace determines ownership;
otherwise the active profile does. Unattributable rows fail closed for multiplex
but retain single-profile legacy compatibility. No new owner stamp is fabricated.

Explicit resume validates the target and every compression ancestor to be rebound,
then writes outgoing reset promotion, target reopen (including legacy reset-child
stabilization), existing full compression-lineage peer metadata, and the selected
route in one SQLite transaction. All authorization checks occur inside that write
transaction. Preserve stronger existing compression lineage exclusions, recovery
end-reason rules and leases. Publish memory routing only after durable success;
on ownership denial or transaction failure, old route and session rows survive.
DB-less single-profile JSON fallback stays available with rollback on save error;
DB-less multiplex fails closed. Existing routing generation ordering is retained.

Tests use real SessionDB and SessionStore in temporary fixtures; only controlled
failure injection and profile identity patches are permitted. No external effects,
installs, live configuration, voice, retired or archive-only work.


# Batch 2 credential contract — before mutation

Scope: FC-17-E13/E14/E15/E16 only; approved source/test work. Historical reference is 2945588a014543d47c9e5e4a0d92ba6e361387c1. No live state, installs, push or staging.

E13: retain shared bounded executor (the historical pool is a thread executor, not a credential pool). Profile identity is context-local and copied for every submission; home-keyed pool proliferation is unnecessary to the isolation outcome. Qualify concurrent contexts without replacing G17R's qualified registry or global drain contract.

E14: every direct run_job call obtains a fresh private profile scope and resets it even on failure; outer execution/delivery scope also refreshes privately. Neither no_agent nor model execution reloads process dotenv or clears other homes' caches. Startup import calls reached inside a scoped run must also avoid process dotenv mutation. Keep single-profile shell credential fallback and fail-closed multiplex behavior.

E15: refresh returns a mapping, leaving token lifetime explicitly owned by callers. Child environment resolution follows get_secret: overlay in single-profile mode; process-global allowlist plus active profile secrets in multiplex mode; fail closed if multiplexed but unscoped. Never overlay secrets after the existing subprocess sanitizer or weaken durable child mutation refusal. Cron scripts use scoped environment before sanitizer. Bot-chat delivery scopes its explicitly selected destination profile and propagates correct home; no source-profile credentials cross profile selection.

E16: extend existing private hydrate path with per-home refresh under its existing lock; invalidate only this home's applied/snapshot state, preserve source fail-open behavior and .op.env bootstrap. A scoped load_hermes_dotenv invocation returns private load metadata without touching os.environ, config bridges or files. Unscoped single-profile startup remains unchanged. No new generic loader framework.

Tests: real temp profile dotenv/config/filesystem, real imports and real safe script child when feasible; fake external-source/backend effects. Canonical interpreter read-only, task runner clean isolated environment, unittest tests in tests/. Human UAT remains pending.


# Batch 2 nonvoice G17 transport contract — before mutation

E03: capture runner launch home/name; unrouted operations use that home even under
another task's ContextVar. Explicit missing/invalid runtime profiles fail closed.
Agent cache identity includes resolved runtime home; stable home keeps byte-stable
signature. Existing credential/config/skip_context cache dimensions remain.
E04/E06/E24: Base adapters capture construction home. Discord durable tracker,
command-sync state and pairing reads stay on it. Existing source route precedence
remains, with owning profile stamped at build_source time before busy/session
routing. Source transport provenance stays in-process, never serialized.
E05/E20/E21/E22: a retained transport reference must resolve to that registered,
running transport; a stale/unregistered ref cannot fall through to another bot.
Pairing grants, requests, rate-limit and unauthorized-DM policy use the transport
owner. Missing secondary pairing stores never borrow the default store. Relay
routing exemption is preserved.
E23: all six nonvoice Discord control views capture pairing home and transport
authorization flags at construction from their adapter. Delayed interactions
cannot borrow a routed profile's pairing grant or allow-all settings. Existing
user/role/admin/deny gates remain; no new model tools or transport effects.
Scope setup must restore home on credential hydration/build failures. Credential
helper implementation is owned by the credentials subtask. No live state or
archive/voice/retired consumers. Brian staging approval already granted; Critical
risk classifications unchanged. Test using real temporary stores/entrypoints.
