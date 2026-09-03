# Incident evidence closure lifecycle

Hermes Agent does not produce incident evidence directories. This is a
closure-only, opt-in contract for an operator who has already created an
explicit incident directory. It never scans by age, never discovers incident
directories, and never defaults to `~/.hermes`.

## Lifecycle

1. During investigation, do not invoke the closure utility. Record every live
   forensic shell, worker, or lease in `live_access`.
2. Before closure, write the closure report, reproducer, patch evidence, and
   reference evidence. Hash each declared artifact.
3. Set `incident.status` to exactly `closed`, populate `closed_at`, and clear
   both `live_access.holders` and `live_access.leases` only after independently
   verifying there is no live access.
4. Review the dry-run plan for the one explicit directory.
5. Only after that review, use both `--apply` and `--confirm-closed`. The
   utility can act only on manifest-declared `full_state_sqlite` artifacts.

The manifest itself, closure report, reproducer, patch, and reference evidence
are mandatory preserved records. An artifact with `retain: true` is never
removed or compacted. Any artifact other than `full_state_sqlite` must use the
`retain` disposition.

## Manifest schema v1

The manifest filename is always `incident-evidence.json` at the root of the
explicit incident directory.

```json
{
  "schema_version": 1,
  "incident": {
    "id": "INC-2026-001",
    "status": "closed",
    "closed_at": "2026-08-10T10:00:00Z",
    "closure_report": "closure-report.md",
    "retention_policy_version": "2026-08-v1"
  },
  "live_access": {
    "holders": [],
    "leases": []
  },
  "artifacts": [
    {
      "path": "closure-report.md",
      "kind": "closure_report",
      "sha256": "a 64-character SHA-256 digest",
      "disposition": "retain"
    },
    {
      "path": "reproducer.md",
      "kind": "reproducer",
      "sha256": "a 64-character SHA-256 digest",
      "disposition": "retain",
      "retain": true
    },
    {
      "path": "patch.diff",
      "kind": "patch",
      "sha256": "a 64-character SHA-256 digest",
      "disposition": "retain"
    },
    {
      "path": "references.md",
      "kind": "reference",
      "sha256": "a 64-character SHA-256 digest",
      "disposition": "retain"
    },
    {
      "path": "state.sqlite",
      "kind": "full_state_sqlite",
      "sha256": "a 64-character SHA-256 digest",
      "disposition": "compact"
    }
  ]
}
```

All artifact paths are relative to the incident directory; absolute paths and
parent traversal are refused. The utility verifies every declared checksum,
requires the closure report to be a checksummed `closure_report` artifact, and
refuses any SQLite file that is not declared in the manifest. `compact` uses
SQLite `VACUUM INTO` and atomically replaces only the declared snapshot. On
successful apply, its checksum is refreshed in the manifest. `remove` is
allowed only for a manifest-declared `full_state_sqlite` artifact and removes
that artifact declaration after deletion.

## Runbook

Dry run first (the default and safe mode):

```bash
python3 scripts/close_incident_evidence.py /absolute/path/to/INC-2026-001
```

Apply only after the dry-run plan and closure evidence have been reviewed:

```bash
python3 scripts/close_incident_evidence.py /absolute/path/to/INC-2026-001 \
  --apply --confirm-closed
```

The command returns JSON and exits nonzero for every refusal. Refusals include
open, active, investigating, unknown, or missing status; missing closure
metadata; absent/invalid manifest; incomplete/checksum-mismatched required
evidence; any live holder or lease; unmanifested SQLite snapshots; and an apply
without the explicit closure confirmation.

Do not add this utility to a periodic cleanup job. It is intentionally an
operator-invoked closure action for one named directory.
