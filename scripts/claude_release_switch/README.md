# Finite release-switch controller

`controller.py` executes an externally approved `stage`, `promote`, or `rollback`
packet against an exact canonical repository and user-systemd service. It is an
independent finite runner with an `ExecStopPost` recovery guard, not a Hermes tool,
plugin, daemon, scheduler, or replacement release framework. The default command
performs preflight only. `--apply` submits a transient supervisor; submission is
not release success. Read its durable result after the unit finishes.

This candidate implements real exact-service lifecycle and Git operations. Its
executable qualification uses disposable Git repositories and user-systemd units;
**application, authentication, approval, CI and backup evidence in those tests is
simulated**. No live gateway stop/start, production source switch, backup,
credential/config repair, permanent installation, or release was performed here.
The deployment owner is `t_ddd2e9dc`; parent review precedes deployment.

Brian's approved repeated-qualified-batch window is
**2026-09-06T20:38:15-07:00 through 2026-09-07T04:38:15-07:00**. It does not activate
this candidate or waive per-batch qualification. Each batch needs a fresh packet,
state directory, unique controller unit and external exact-packet approval. The
runner never schedules another batch or retries one automatically.

## Authority and execution

The operator supplies `--authority` as the externally approved SHA256 of the
exact packet bytes. Calculating that hash does not confer approval. Ang/operator
approval, writer holds, CI, backup verification, compatibility and credential
permission are external facts; this tool validates their frozen receipts and
cannot manufacture them. There is no production packet with placeholder approvals.

Claude receives the full frozen execution plan, runbook and durable operation
journal. Large source/ref/config inventories are represented in the model's plan
by their counts and SHA256 digests; the runner validates the complete inventories.
Claude has no native tools, Bash, skills, MCP, project settings or file-edit path.
It proposes only `{"op":"ID"}`. Only the next exact ID is accepted, and acceptance
actually triggers that operation. Arguments, executable paths and settings cannot
come from model output. Malformed output, abort, quota/auth loss or timeout ends
forward execution. Recovery makes no model calls.

Trusted checks have complete operator-frozen argv, cwd, environment and timeout.
Their executables, Python script files and direct shebang interpreters are hashed.
Direct commands (including Git, service commands, credential helpers and SSH)
and interpreters must pass effective-UID execute access before stop. Python input
scripts need readable pinned bytes; their intended 0600 mode remains supported.
Shells, `env`/`sudo` wrappers, interpreter `-c`/`-m`/`-e`, eval and command escapes
are rejected in check argv. A trusted installed script remains trusted code: its
imports, credential dependencies and child executables must be reviewed and
included in the artifact manifest. This is not an OS sandbox for hostile scripts
or other same-UID processes. The model has no mechanism to supply such scripts.

Generated lifecycle commands are exactly `/usr/bin/systemctl --user stop UNIT`
and `start UNIT`; they cannot substitute a fixture wrapper or another unit.
Generated Git commands disable hooks, fsmonitor and recursive submodule updates.
Stage uses normal `git switch staging`. Promote switches main, checks its recorded
parent, merges `--ff-only EXACT_SHA`, and pushes `EXACT_SHA:refs/heads/main` without
force to the explicit frozen remote. Readback also follows ambiguous push failure.
No reset, clean, dependency installation, conflict resolution or data restore exists.

## Install downstream, outside switched source

These are instructions for the separately authorized deployment owner, not actions
performed by this task. Set `RELEASE_INSTALL` to a new private versioned directory
under `/home/brian/.hermes/profiles/ang/state/release-switches/`; set
`RELEASE_STATE` to a different new per-batch directory there. Neither may be inside
the source checkout. From the reviewed candidate checkout:

```bash
umask 077
install -d -m 700 "$RELEASE_INSTALL" "$RELEASE_STATE"
install -m 600 scripts/claude_release_switch/controller.py \
  scripts/claude_release_switch/health.py "$RELEASE_INSTALL/"
install -m 600 /home/brian/Documents/Runbooks/claude-release-switch-controller.md \
  "$RELEASE_INSTALL/runbook.md"
```

Copy each reviewed standalone offline/drain/health check into the same trusted
installation, preserving any separately required executable mode. Do not install
`fixture.py` for deployment. Pin all copies and interpreter/executable identities
in the packet. No permanent controller unit is required. The existing exact service
and its startup evidence producer are qualified under the approved lifecycle design;
this controller neither installs nor changes that service.

## Prepare and qualify one packet

The strict, executable schema is `SCHEMA` in `controller.py` (version 2). Unknown
keys, duplicate JSON keys, wrong types including bool-as-int, changed artifacts,
unsafe modes or altered command plans fail closed. All state/receipt files are
0600; state and installation directories are 0700. The packet contains:

| Field | Required preparation |
| --- | --- |
| `order`, `profile`, `operation` | Actual approved order, exact profile, named batch and verification owner; stage never implies promote. |
| `repo`, `repo_identity` | Canonical absolute path and actual device/inode, owned by executor UID; `.git` is the canonical directory. |
| `current`, `candidate`, `rollback` | SHA, tree, branch and full file SHA256/mode inventory from `revision(repo, sha, branch)`. Current branch may be empty for detached state; candidate is staging, preserved rollback branch is rollback. Regular tracked files are supported; gitlinks/symlinks fail closed. |
| `local`, `authoritative`, `extra_refs` | Exact main/staging/rollback locals, authoritative main/staging, and every additional ref/SHA. Additional unrelated worktrees are allowed; none may hold main, staging or rollback. |
| `git_config`, `untracked` | Exact local config lines and every ignored/untracked filename. Each inventory entry has path, mode, classification (`backed-up` or `reproducible`) and reason. Backed-up entries pin SHA256 (symlink target text for symlinks); reproducible entries have empty SHA256. Names/modes remain checked. Backup receipts cover non-reproducible files. No reset/clean to satisfy a denial. |
| `remote`, `git_route` | Explicit local bare path, credential-free HTTPS URL, or SSH `user@host:path`. Route has isolated private absolute `home`, and one pinned absolute credential `helper` (HTTPS) or `ssh` executable (SSH); unused fields are empty strings. Local bare routes have both empty. |
| `unit`, `unit_definition_sha256`, `launcher`, `launcher_sha256`, `baseline` | Exact service, hash of `raw([SYSTEMCTL, '--user', 'cat', unit], '/', env=system_env()).encode()`, launcher bytes, actual PID/kernel starttime/cgroup/full argv from `proc(MainPID)`. Unit fragment/drop-ins and baseline interpreter must be artifacts. Reload-needed or definition drift blocks execution. |
| `installation`, `controller_unit`, `lock`, `lock_identity` | Installed copy location, unique independent service name, actual existing lock path/device/inode. The same external sole-writer hold must cover all writers and recovery handoff. |
| `window` | Integer Unix start, abort, expiry, recovery deadline and reserve. Start < abort < expiry <= recovery deadline. Reserve covers frozen recovery command timeouts plus at least 40 seconds for validation/checks. Fencing has a separate allowance of twice reserve (two possible systemd termination waits). Expiry minus abort must cover three times reserve plus 10 seconds dispatch margin; recovery deadline is no earlier than expiry. ExecStopPost has its own full reserve timeout, bounding aggregate commands/checks; slow or stuck validation fails closed rather than extending it. Measure real timings and allow more when required. |
| `checks`, `command_timeout` | Frozen drain/offline/smoke/recover-offline/recover-smoke command records. Each has `id`, `argv`, `cwd`, closed `env`, integer `timeout` (1–300 seconds). `command_timeout` sets generated Git/service command limits (1–300). Slow qualification runs before outage. |
| `commands`, `recovery` | Generate from `command_plan(packet)` after the other fields are final; exact equality is enforced. |
| `drain_path`, `health_path` | Exactly `RELEASE_STATE/drain-proof.json` and `RELEASE_STATE/runtime-health.json`. The runner writes validated command stdout there. |
| `runbook`, `artifacts` | Runbook path/hash; artifact entries with absolute path, SHA256, observed UID (`artifact_uid(path)` normalizes namespace-mapped system root to 0) and numeric mode. Include controller, checks/imports, runbook, Git, systemctl, systemd-run, Python, launcher/interpreter, service files and credential-route files. Group/world-writable artifacts are refused. |
| `model`, `receipts` | Model kind `claude`, name `claude-sonnet-5`, bounded timeout; all receipt references described below. `fake` is a deterministic test driver, not actual inference evidence. |

Git inherits no operator/provider environment. It uses its isolated HOME,
`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, no terminal prompting and
an explicitly reset credential-helper list. SSH receives only its pinned transport;
its reviewed wrapper must select the actual isolated identity/config/known-hosts
route. Pin those dependencies too. Verify the intended actor and ordinary push
permission before stop and record that evidence. Neither Claude's HOME nor a
successful `ls-remote` proves write permission. Do not copy or repair credentials.

Receipt references are `{path, sha256}`, with paths `RELEASE_STATE/KIND.json`.
Every receipt has exactly `order`, `kind`, `candidate`, `rollback`, `repo`, `unit`,
`profile`, `expires`, `evidence`, `approved`, `facts`. Target/order/SHAs must match;
`approved` is true only on externally validated evidence, and expiry must cover
recovery. `facts` contains nonempty strings with these exact keys:

| Kind | Fact keys |
| --- | --- |
| authority | approver, provenance, batch, recovery, contact |
| ci | exact_sha, result |
| review | exact_sha, verdict |
| backup | coverage, restore_verified |
| compatibility | candidate, rollback, dependencies, persistent_state |
| drain | mechanism, all_source_consumers |
| sole_writer | owner, hold, writers, recovery_handoff |
| git_credentials | route, actor, permission_verified |
| readiness | probe_path, probe_sha256, auth_method, model, cli_sha256 |

`git_credentials.facts.route` is `digest(encoded(packet['git_route']))`. CI/review
exact_sha equals candidate. Readiness pins the preserved successful no-tools probe
JSON and CLI bytes, `auth_method=claude.ai`, and the requested execution model.
Both readiness and proposal accounting require `claude-sonnet-5` and allow only
the verified auxiliary identity `claude-haiku-4-5-20251001` alongside it. The
preserved successful probes and both real sequences contain both identities.
Unknown identities, absent execution model, errors and malformed output are denied;
auxiliary accounting grants no operation authority. The previous
actual probe and two actual Claude sandbox sequences remain in this task's evidence;
**do not rerun them for this correction**. Preflight and supervisor startup validate
the receipt without doing inference. Claude is called only for actual release op
proposals. The explicit `probe` subcommand remains available for a future separately
requested readiness qualification; it is not part of this task's tests.

The real updater's PID/time marker is not this flock. Either use the actual shared
mutation lock with all writers participating, or establish an external sole-writer
hold that covers updater, editors, jobs, workers and source consumers through
recovery. This runner's flock alone makes no claim to coordinate those tools.

## Drain and health contracts

Drain stdout is JSON with `active_jobs: 0`, exact `unit`, baseline `pid` and
`starttime`, and integer `observed` time inside the approved window. It must come
from the qualified lifecycle mechanism after draining **all** work; raw systemctl
stop is not drain evidence. The runner validates it before stop and again before
source mutation. Quiescent state (`inactive`, or `failed` with MainPID zero), baseline PID disappearance and empty gateway cgroup
are separately required.

`health.py STARTUP_JSON LIVE_JSON EXACT_UNIT` is the standalone read-only contract
hook. It reads private evidence and systemd/proc identity and prints health JSON.
Startup evidence has `pid`, kernel `starttime`, `sha`, canonical `source`, complete
loaded `bytes` inventory and `executable_sha256`. The qualified startup producer
must capture these from the actual starting process/executable and loaded source,
not from mutable HEAD inspected later. Freeze and qualify that producer with the
service installation; never invent this record during packet preparation.

Fresh live evidence has matching `pid`/`starttime`, integer `observed` (at most
30 seconds old), and `platform`, `scheduler`, `persistence`, `sessions`, each `ok`
only after its real check passes. Freeze the bounded smoke command that obtains
those observations and invokes the hook. The hook does not create connectivity,
scheduler or backup evidence. In the disposable fixture only, their producers are
mocked; process/systemd checks and the health hook itself execute normally.

The controller requires a new PID/starttime, exact baseline argv/cgroup, pinned
interpreter hash, the expected startup SHA/source/bytes, all four health checks,
and final working-byte/ref verification. A stale receipt, service `active` alone,
or HEAD read after startup cannot prove success.

## Launch and deterministic recovery

After external approval of the exact packet digest, the deployment owner runs:

```bash
/usr/bin/python3 "$RELEASE_INSTALL/controller.py" "$RELEASE_STATE/packet.json" \
  --authority "$APPROVED_PACKET_SHA256"
# Separately authorized activation only:
/usr/bin/python3 "$RELEASE_INSTALL/controller.py" "$RELEASE_STATE/packet.json" \
  --authority "$APPROVED_PACKET_SHA256" --apply
systemctl --user show "$CONTROLLER_UNIT" -p ActiveState -p Result
cat "$RELEASE_STATE/result.json"
cat "$RELEASE_STATE/journal.jsonl"
```

The supervisor proves MainPID/cgroup independence, no gateway lifecycle coupling,
Restart=no, controller-only KillSignal=SIGKILL and FinalKillSignal=SIGKILL,
SendSIGKILL=yes, TimeoutStopFailureMode=kill, exact TimeoutStopSec=reserve, no
ExecStop, control-group killing, external cwd and recovery guard before writing
`ready.json`. It holds the lock through forward work. Stop intent and a durable
`recovery-required` result are fsynced **before** systemctl stop. Real clock checks
surround evidence validation and every command; complete timeouts must fit the
abort/reserve or recovery deadline. systemd RuntimeMaxSec independently ends stalls.

On controller/child loss or RuntimeMaxSec, systemd sends SIGKILL to the controller
cgroup immediately. The executable regression proves this handles a SIGSTOPed
supervisor with a TERM-ignoring setsid child before recovery. Gateway graceful
stop is unchanged. SIGKILL cannot guarantee removal of kernel-stuck processes:
the guard still requires its cgroup to contain only itself and refuses recovery
if any descendant survives. There is no later retry after such a refusal.
The guard reacquires the same lock inode under the same external writer hold,
verifies topology and the empty-writer proof, and executes these frozen steps:

1. Stop the exact unit; prove inactive, baseline gone, cgroup empty and drained.
2. Require clean source and frozen refs; normal `git switch --detach ROLLBACK_SHA`.
3. Run recover-offline; recheck known-good bytes, refs, compatibility and deadlines.
4. Start the same unit; run recover-smoke and verify its new known-good process.
5. Record `rolled-back`. Published main is preserved; later reconciliation requires
   a separately reviewed nonforce recovery commit and new qualification.

No uncertain rollback is force-started. Failure to fence, lock/receipt/artifact drift,
dirty bytes, incompatible state or exhausted deadlines leaves `recovery-required`.
Use the exact non-mutating `operator_command` stored in that result:

```bash
/usr/bin/python3 "$RELEASE_INSTALL/controller.py" "$RELEASE_STATE/packet.json" \
  --authority "$APPROVED_PACKET_SHA256" --inspect-recovery
```

Read back the actual service and controller states; after startup failure or loss
of the guard, do not assume the service is stopped. Do not invoke internal
`--recover` in an ordinary shell: takeover fencing will reject it. Do not edit the
consumed packet, reuse its approval, release the external writer hold prematurely,
or rerun apply to bypass a denial. If automated recovery cannot finish, the named
external owner uses the frozen recovery order and contact route to qualify the
next action. Host/user-manager loss requires that owner; no reboot daemon exists.

State replacement fsyncs file and directory; the journal appends and fsyncs. Logs
retain controlled event/error categories, op IDs and SHA/PID evidence, not command
stderr, model text or credentials. Same-UID tampering is outside this trust boundary.
Cgroups fence setsid descendants; they do not isolate a hostile trusted executable
that can use the user bus to create another service. Qualify the trusted executable
set, external writer hold and external recovery coverage for the deployment host.

## Disposable executable qualification

Only the fixture driver enforces task-named sandbox admission and injects faults;
production packets have no fault fields or fixture admission contract. Choose a
new root under this task's evidence directory:

```bash
umask 077
TASK_ROOT=/home/brian/.hermes/kanban/boards/operations/workspaces/t_acd2041b/evidence/t-acd2041b-demo-UNIQUE
python3 scripts/claude_release_switch/fixture.py "$TASK_ROOT" create --operation promote
```

Use its printed installed-controller path, packet path and **simulated** authority
with the preflight/apply commands above. `--operation stage` and `rollback` use fresh
roots. Faults such as `--fault-point push --fault-kind supervisor-loss` are fixture
behavior outside the packet; they never change the production executor's sequence.
Cleanup stops only that fixture's exact units, removes its temporary test service
file and retains its repository/evidence:

```bash
python3 scripts/claude_release_switch/fixture.py "$TASK_ROOT" cleanup
```

Run the focused suite in the isolated test environment owned by this task:

```bash
HERMES_PYTHON=/home/brian/.hermes/kanban/boards/operations/workspaces/t_acd2041b/test-venv/bin/python \
  scripts/run_tests.sh tests/scripts/test_claude_release_switch.py -q --file-retries 0 --file-timeout 900
```

Git stage/promote/detached rollback, nonforce push/readback, extra refs/worktrees,
classified files, exact service actions, submitter exit, supervisor SIGKILL,
timeout/setsid fencing and deadline recovery are real. The isolated fake SSH
transport runs real Git upload/receive-pack locally; it proves explicit route/env
propagation, not remote authentication. Linux tests explicitly skip without a user
manager; a skip is not topology evidence. No actual model is called by the suite.
