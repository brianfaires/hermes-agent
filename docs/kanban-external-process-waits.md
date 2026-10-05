# Existing Kanban waits for supervised terminal CLI output

The external-process variant requires Linux user-systemd/procfs and fails
closed before file inspection on unsupported platforms; native waits are unchanged.

`kanban_block(kind="dependency", wait=...)` accepts the existing native
`{board, task_id, run_id}` result reference unchanged. The alternative is:

```json
{
  "next_owner": "default",
  "executor": "ang",
  "expected_effect": "Inspect terminal CLI output and current authority",
  "resume_condition": "result",
  "recheck_at": 1791200000,
  "result_ref": {
    "kind": "external_process",
    "unit": "release-source-unique.service",
    "invocation_id": "0123456789abcdef0123456789abcdef",
    "release_id": "release-unique",
    "phase": "source",
    "candidate_sha": "0123456789abcdef0123456789abcdef01234567",
    "known_good_sha": "abcdef0123456789abcdef0123456789abcdef01",
    "result_basename": "last-message.txt"
  }
}
```

The executor must equal the initiating task assignee and native run profile
(`ang` in this example). Foreign executor file reads are rejected before
inspection; native cross-profile run references retain their existing behavior.
The authorized executor must already have launched an independent transient
systemd **user service**, with an explicit `HERMES_HOME` environment property
matching its existing profile. Environment markers alone are insufficient:
live admission checks `/proc/PID/stat` start ticks and `/proc/PID/cgroup` against
MainPID, ExecMainPID, supervisor start time and ControlGroup. The process group
and cgroup must be independent of the caller and native control worker. PID,
start ticks, cgroup and invocation identity are frozen in native metadata and
compared on reconciliation. Use the actual InvocationID from that service.
The reconciler only reads `systemctl --user show`; it never launches anything.
Keep the unit inspectable after exit (for example, `RemainAfterExit=yes` for
successful exit; do not use `--collect`). Automatic restarts change invocation
identity and cannot satisfy the old wait. Services must expose task accounting;
a service with surviving processes cannot provide a terminal receipt.

The direct CLI final-message/output file (such as Codex's `-o` destination) is
`<executor-home>/state/release-switches/<release_id>/<result_basename>`.
The state/release directories must be private, owned by the current UID;
profile directories must not be group/world writable. No component may be a
symlink. The file must be private, regular, singly linked, nonempty, at most
1 MiB, and modified after this service invocation started. It is read through
held directory descriptors starting at the first real path component (never
opening `/`), with change detection and supervisor revalidation.
A clock discontinuity can conservatively reject output. No directory scanning
or alternate output-file fallback occurs.

Scheduling freezes the entire wait in the existing native run metadata and
ends that run as `scheduled`, without spending failure/block budgets.
External-process waits allow only `result`; `accepted` and `launched` remain
native-run-only conditions. Live process evidence never enables owner transfer.
`result` ignores early output until the service and all its tasks are terminal.
A successful process exit records SHA256, byte count,
unit result, and exit status in native metadata/events; normal reconciliation
then transfers to `next_owner` and ordinary dispatch applies its gates and pins.
Private output bytes and arbitrary error text never enter Kanban metadata or
events. The receipt is untrusted process evidence, **not release approval or proof that
an output's claims are true**. There is no fake external native task/run.

A crash records `process_failed` once, even without output, and emits an
`external_wait_due` receipt while retaining the current owner and scheduled
status. Existing explicitly authorized read-only continuation can inspect the
native run; the failure does not authorize execution or certify success.
Missing/reused units and invalid successful-result files fail closed. Existing
holds, owner/run freshness, ESTOP, parent gates, and retry budgets remain in
force. Model/provider/subscription pins are unchanged.

Focused tests cover private file I/O, frozen identities, owner gates, and a
real detached process rejected because it still shares the controller cgroup.
The parent-only harness below exercises actual systemd, service survival after
initiator exit, a fresh dispatcher, correct owner/pins and quiet dedup. It uses
a deterministic child and a spawn sentinel, not a model execution claim.

Parent Ang, on the authorized host with a user bus, can run:

```bash
HERMES_PYTHON="$PWD/.rook-test-venv/bin/python" \
HERMES_HOME="$(mktemp -d /tmp/item6-parent-XXXXXX)" PYTHONPATH="$PWD" \
scripts/run_tests.sh tests/hermes_cli/test_kanban_external_process_parent.py \
-k test_parent_systemd_process --file-retries 0 --file-timeout 100
```

The test is skipped unless that exact `-k` opt-in is present. It creates only a
UUID-named transient test service and pytest-temporary owner state, retains the
unit until receipt validation, then stops/resets only that test unit. No profile
configuration or credential files are copied or changed. User-bus addressing is
reconstructed under `/run/user/<uid>` inside this explicit parent test only.

Actual direct Codex boundary remains a separate parent acceptance step: use an
authorized independent transient service, a private same-owner release output
path, read-only Codex execution with `gpt-6-astra`, Fast OFF/default, and the CLI
`-o` final output destination. Keep the service running while the initiating
native run registers its exact wait; retain the unit after exit. Use existing
authorized authentication in place, with no credential movement or paid fallback.
Reconcile through the existing dispatcher and verify the digest matches the
actual private `-o` file. Invocation/PID proof is evidence, not execution
permission. This sandbox does not run that service or claim that acceptance.
