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

Any dated approval window recorded in prior task notes is historical evidence
only. It is not operative approval for a future batch. Each batch needs a fresh
packet, state directory, unique controller unit, external all-consumer hold and
external exact-packet approval. The runner never schedules another batch or
retries one automatically.

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
Stage uses normal `git switch staging`, checks its frozen local parent, then
`git merge --ff-only EXACT_SHA` while drained and stopped. Authoritative staging
must already be that exact candidate; local staging may still be its frozen
ancestor (including the currently checked-out old staging). Exact refs, branch,
index/tree and working bytes are checked after the merge and before start.
Promote switches main, checks its recorded
parent, merges `--ff-only EXACT_SHA`, and pushes `EXACT_SHA:refs/heads/main` without
force to the explicit frozen remote. Readback also follows ambiguous push failure.
No reset, clean, dependency installation, conflict resolution or data restore exists.

Stage and promote may accept either relative local main / authoritative main
order only when both frozen endpoints are ancestors of candidate and the two
commits lie on one chain, with all objects already available locally. Stage
leaves main unchanged; promote merges the exact candidate and pushes normally.
Rollback retains the preflight main-alignment requirement. Recovery accepts only
the frozen old/new endpoints of the branch this operation could advance,
including a merge that completed before its journal write; unrelated refs and
remote staging stay frozen. Only promotion recovery permits ambiguous
publication of candidate main.

The external runbook's stage wording must be reviewed and aligned with this
switch-plus-fast-forward plan before a deployment packet is approved. This source
change does not amend that runbook or authorize any deployment.

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
  scripts/claude_release_switch/health.py \
  scripts/claude_release_switch/launch_gateway.py \
  scripts/claude_release_switch/runtime_health.py \
  scripts/claude_release_switch/runtime_observation.py \
  scripts/claude_release_switch/drain_proof.py "$RELEASE_INSTALL/"
install -m 600 /home/brian/Documents/Runbooks/claude-release-switch-controller.md \
  "$RELEASE_INSTALL/runbook.md"
```

Copy each reviewed standalone offline/drain/health check into the same trusted
installation, preserving any separately required executable mode. Do not install
`fixture.py` for deployment. Pin all copies and interpreter/executable identities
in the packet. No permanent controller unit is required. Service-definition changes
remain Ops-owned and approval-gated; this source task does not install or change
the live service.

The reviewed startup producer is `launch_gateway.py`, with the adjacent
`runtime_observation.py`. Final `ExecStart` must use the existing venv interpreter
with **`-I -S -B -X pycache_prefix=/dev/null` present before the script**. These flags prevent inherited
`PYTHONPATH`/`PYTHONHOME`, user-site, venv/system `.pth`, and automatic
`sitecustomize` execution before the bootstrap. `-B` prevents writes and the non-directory `/dev/null` cache prefix prevents even startup stdlib cache reads. Bytecode-only imports are refused, and source/native imports outside tracked source or explicitly pinned dependencies fail closed. The bootstrap removes inherited
Python import controls from child environments too. It never calls `site.main()`
or `addsitedir()` and never recreates the venv.

Ops must prepare and review a private `dependencies.json` containing exactly:
`paths` (ordered canonical absolute directories for the selected interpreter's
stdlib, lib-dynload, existing venv site-packages and any executable plugin roots), `files` (absolute path to
SHA256 for every regular file below those directories except `__pycache__`/`.pyc`),
and `observer_sha256` (the installed `runtime_observation.py` hash), plus
`venv_config` (the selected existing venv's canonical `pyvenv.cfg` path, or an empty
string for a system interpreter). Include that config file in `files`. The installed
launcher's `dependency_files(paths, venv_config)` implements that inventory; choosing the paths
is a reviewed installation decision, never inferred by executing `.pth` files.
The explicit dependency paths may live inside the checkout (the normal `.venv`
layout), but none may classify any tracked current/candidate/rollback source file
as a dependency. The loader verifies each dependency source against its separate
inventory. On Python versions where `-S` disables venv prefix detection, the
bootstrap restores `sys.prefix`/`sys.exec_prefix` from the pinned config path and
checks it against the actual selected interpreter; it does not execute the config.
Pin the manifest, every inventoried dependency, observer, bootstrap, interpreter,
and service fragment/drop-ins in the packet artifact list. Maintain the external
source/dependency hold through recovery. Stdlib dependencies are trusted during
bootstrap startup and must be pinned before service activation.

```bash
/home/brian/.hermes/hermes-agent/.venv/bin/python -I -S -B -X pycache_prefix=/dev/null "$RELEASE_INSTALL/launch_gateway.py" \
  --repo /home/brian/.hermes/hermes-agent \
  --startup-json "$RELEASE_STATE/startup.json" \
  --dependencies "$RELEASE_INSTALL/dependencies.json" \
  --dependencies-sha256 "$REVIEWED_DEPENDENCIES_SHA256" \
  -- -m hermes_cli.main gateway run
```

The bootstrap inventories clean tracked bytes, then installs a direct source
compiler before importing `hermes_cli.main` and `gateway.run`. Ordinary and lazy
`SourceFileLoader` imports compile the verified bytes directly, bypassing pyc.
Preloaded repository modules, untracked source, repository native modules,
bytecode-only imports, and candidate-owned modules resolving elsewhere
are refused. The startup record separates the full **on-disk** `bytes` inventory
from `loaded`, the modules actually compiled and executed so far. Status readback
refreshes `loaded` after lazy imports. It does not claim the whole inventory is
loaded, or defend against malicious same-UID code bypassing Python import loaders.
The external frozen source hold is required.

**Legacy baseline installation gate:** the reported live baseline still uses
`python -m hermes_cli.main gateway run`. This controller deliberately refuses it;
it does not silently compare that process with the new launcher argv. Installing
and loading the new unit definition alone does not change the running process or
add the observer. Before a release packet can use this implementation, Ops and
Brian must separately authorize and execute one **bootstrap installation and
restart on unchanged known-good source**, under their own qualified all-consumer
drain, source/dependency hold, backup and recovery order. Verify the new observer,
exact argv and new PID/starttime, then freeze a new release packet. A later release
switch is a second stopped transition. Neither bootstrap restart nor a combined
single-transition migration is authorized or implemented by this preparation.
If the bootstrap cannot start, the separately approved installation recovery must
restore the saved original unit definition and start unchanged known-good source;
this controller cannot recover a bootstrap it never admitted. The old running
process cannot supply `release_observation`, so its first drain must be qualified
by that separate installation owner, not by generating a success receipt here.

### Bootstrap installation guard

`bootstrap_recovery.py` is the separate, deterministic one-shot guard for that
legacy-baseline bootstrap installation only. It performs no source release switch
and makes no model calls. It installs one exact candidate service configuration
file, runs packet-frozen reload/drain/stop/start/health commands, and writes a
durable `bootstrap-active` result only after the same service reports the
approved bootstrap target. On any failure after configuration mutation, the
transient supervisor's `ExecStopPost` runs `--recover`: it fences controller
descendants, retains the same lock, proves the gateway unit quiescent, removes
only the exact candidate bytes, reloads, starts the approved legacy target, and
requires fresh legacy health. It never restores or overwrites persistent data.

Install the bootstrap guard alongside the release controller only for an
approved bootstrap packet:

```bash
umask 077
install -d -m 700 "$RELEASE_INSTALL" "$RELEASE_STATE"
install -m 600 scripts/claude_release_switch/controller.py \
  scripts/claude_release_switch/bootstrap_recovery.py \
  scripts/claude_release_switch/bootstrap_install_packet_template.py \
  scripts/claude_release_switch/bootstrap_runtime_bindings.py \
  scripts/claude_release_switch/runtime_health.py \
  scripts/claude_release_switch/drain_proof.py \
  scripts/claude_release_switch/health.py \
  "$RELEASE_INSTALL/"
```

Build the production packet from exact reviewed inputs outside the checkout.
The input must already contain reviewed hashes, service identity, the
candidate-drop-in source/live paths, lock identity, window, artifacts, and
`bootstrap_bindings` paths for `hermes_home`, `hold_json`, `repo`,
`startup_json`, and `live_json`. The helper binds drain and drain clearing to
the bootstrap legacy producer, bootstrap health to `runtime_health.py`, and recovery health to a
legacy systemd/proc/control-socket producer that does not require the new
observer on known-good rollback. For live packets with
`unit_definition_required=true`, include both `baseline_unit_sha256` and
`candidate_unit_sha256`, where each is the SHA256 of
`systemctl --user cat UNIT` in its reviewed phase: before mutation for baseline,
and after candidate installation plus `daemon-reload` for candidate. The helper
fills only the exact production lifecycle/evidence command records:

```bash
/usr/bin/python3 "$RELEASE_INSTALL/bootstrap_install_packet_template.py" \
  "$RELEASE_STATE/bootstrap-input.json" > "$RELEASE_STATE/bootstrap-packet.json"
```

The helper prints the packet SHA256 on stderr. That SHA is not authority until
Ang approves it out of band. After approval names the exact hash:

```bash
/usr/bin/python3 "$RELEASE_INSTALL/bootstrap_recovery.py" \
  "$RELEASE_STATE/bootstrap-packet.json" \
  --authority UNRESOLVED_ANG_APPROVAL_OF_PACKET_SHA256
/usr/bin/python3 "$RELEASE_INSTALL/bootstrap_recovery.py" \
  "$RELEASE_STATE/bootstrap-packet.json" \
  --authority UNRESOLVED_ANG_APPROVAL_OF_PACKET_SHA256 --apply
```

For a live bootstrap packet, set `unit_definition_required` to `true`. The guard
checks the baseline definition at preflight and again after recovery reload, then
checks the candidate definition after candidate install/reload while the original
process is still draining and after bootstrap health. `candidate_unit_sha256` is
an additional packet field because the complete rendered systemd definition is
the reviewed `systemctl cat` output; it cannot be reconstructed safely from the
candidate drop-in bytes alone. The transient fixture qualification sets
`unit_definition_required` to `false` because it deliberately avoids persistent
unit files and user-manager daemon reload; that fixture proves the same-service
configuration/recovery algorithm, not live service-definition restoration.

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
| `unit`, `unit_definition_sha256`, `launcher`, `launcher_sha256`, `baseline` | Exact service, hash of `raw([SYSTEMCTL, '--user', 'cat', unit], '/', env=system_env()).encode()`, installed launcher script bytes, actual PID/kernel starttime/cgroup/full argv from `proc(MainPID)`. The launcher script must be in the baseline service command and artifact manifest, with matching `launcher_sha256`; the legacy-baseline bootstrap gate above applies. Unit fragment/drop-ins and baseline interpreter must be artifacts. Reload-needed or definition drift blocks execution. |
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

`drain_proof.py HERMES_HOME HOLD_JSON REPO UNIT PID STARTTIME
--recovery-deadline UNIX_SECONDS` is the evidence producer **after** the existing
`gateway.drain_control.write_drain_request()` mechanism and the external hold are
established. The frozen check argv must supply the packet's exact recovery
deadline. Hold JSON has exactly `kind="release-admission-hold"`, `repo`, `unit`,
`pid`, `starttime`, integer `observed`, `valid_until`, `recovery_deadline`, nonempty
`owner`, `recovery_owner`, `approved=true`, and `coverage` with the following keys:

- `gateway_turns`, `cron`, `api`, `background_work`: owner-established admission
  barriers, with the concrete mechanism and independently observed quiescence.
- `kanban_workers`, `updater`, `editors`, `source_readers`: exact detached consumer
  population and the mechanism holding each population stopped or excluded.
- `recovery_handoff`: named recovery owner, transfer/contact route and release
  conditions. The owner retains the hold after any failure until recovery is
  verified or a separately authorized handoff completes.

Coverage text records external evidence; it cannot replace busy observation.
`valid_until` must cover the frozen `recovery_deadline`. Both hold freshness and
ownership are checked again after two control observations. The status callback
must report actual draining state, zero live turn/cron/API counts, and no live
background tasks, delegates, terminal processes or completion watchers. Unknown
or unreadable work fails closed even when persisted `active_agents` is zero.
The observer adds no mutation verb and never establishes/releases a hold.

`health.py STARTUP_JSON LIVE_JSON EXACT_UNIT` is the standalone read-only contract
hook. It reads private evidence and systemd/proc identity and prints health JSON.
Startup evidence has `pid`, kernel `starttime`, `sha`, canonical `source`, complete
tracked `bytes` inventory, `loaded` module byte attestations and
`executable_sha256`. The qualified startup producer must capture these from the
actual final gateway Python process/executable and resolved loaded source, not from
mutable HEAD inspected later or a pre-exec inventory. Freeze and qualify that
producer with the service installation; never invent this record during packet
preparation.

`runtime_health.py STARTUP_JSON LIVE_JSON EXACT_UNIT HERMES_HOME` uses a
bounded live control answer from the actual runner. Its installed callback
captures the authoritative profile/home mapping using the existing profile
resolver and checks each profile's real SessionStore handle when opened. Secondary
handles open lazily in production: when absent, the observer checks the existing
profile database read-only, without creating a session or a write-capable handle.
Missing or unreadable databases still refuse readiness. Collection separately
opens each exact `state.db` read-only and checks genuine ticker heartbeat/success
files. Both markers must postdate this launcher generation, so a recent marker
left by the previous process is insufficient. Startup restoration must finish
before the runner reports readiness, and unfinished boot warmup remains busy.
The executable retries incomplete readiness for `--startup-timeout` seconds
(default 90, allowed 0–240); zero makes one collection attempt. Each collection
has its own bounded I/O, so freeze an outer smoke/recovery-smoke timeout with
headroom (120 seconds for the default retry budget), and include that timeout
in the recovery reserve. The default ticker age allowance is 90 seconds because the built-in ticker
runs every 60 seconds; live runner observation is newly sampled on each request.

Supported local transport observations are Discord's ready/open/heartbeat-ACK
predicate, Telegram polling's running application/updater plus actual getUpdates
progress (90 seconds), Slack Socket Mode connection and ping/pong state, and the
API server's serving listener, plus the webhook adapter's nonempty aiohttp
runner sites with every listener serving. This proves local listening readiness,
not external delivery. Telegram webhooks and unknown adapters fail closed.
Feishu remains unsupported: its WebSocket executor future/thread can stay alive
during reconnect and the adapter has no native fresh transport/ACK predicate.
A historical persisted Feishu entry is not an active runner adapter and does not
participate; if Feishu is active at bootstrap it blocks qualification pending
separate transport proof (including its distinct HTTP mode).
These checks inspect existing state and make no credential or provider calls.
Persisted platform transition timestamps are not refreshed or treated as probes.
Required endpoints come from the primary runner's loaded config and the secondary
configs consumed by actual startup/reconnect calls. Unrelated config reads and
historical persisted status do not change that set. Every required enabled
platform must be present and currently healthy; current failed-platform queues
also refuse success. Missing or unreadable required-profile state fails closed.
Adapter-owned session tasks, active session guards and message-processing
background tasks remain busy through post-handler TTS/media/final delivery.
Weak ownership tracking preserves visibility when a failed adapter is removed
from a live map while its processing task still holds it. No adapter processing
task is exempted by a watcher tag; the existing runner exclusion remains only
for its supervised permanent watchers.
The API adapter's permanent orphan-run sweeper is maintenance rather than message
processing. Only the native `APIServerAdapter` coroutine code with that exact
adapter as its bound owner is exempted from the adapter background-task check.
Task names/tags, other coroutine code and foreign owners cannot qualify. This
exception does not apply to session tasks, active guards or queued/inflight runs.
Missing profiles, failed session handles, stale secondary tickers, disconnected
transport, unreadable work state and nonempty work all block success. The same
external observer composition uses existing constructor/callback seams on
candidate and rollback; it does not require patching gateway core.

The controller requires a new PID/starttime, exact baseline argv/cgroup (after the separately approved bootstrap gate), pinned
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
TASK_ROOT=/home/brian/.hermes/kanban/boards/operations/workspaces/t_8ed10dee/evidence/t-8ed10dee-demo-UNIQUE
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

Bounded actual-path qualification lives in `tests/scripts/test_release_actual_path.py`.
It launches the real CLI/runner/control callback/ticker/session store with local
transport stubs in disposable repositories; only systemd ownership lookup is
substituted in that subprocess test. Separate controller wire tests retain their
simulated application producer in `tests/scripts/fixtures/.../wire_launcher.py`;
that fixture is never a production provenance or runtime health attestation.
Select explicit test nodes and a short task-local `--basetemp`; do not replay the
whole controller matrix for this correction. No real model calls are needed.

Git stage/promote/detached rollback, nonforce push/readback, extra refs/worktrees,
classified files, exact service actions, submitter exit, supervisor SIGKILL,
timeout/setsid fencing and deadline recovery are real. The isolated fake SSH
transport runs real Git upload/receive-pack locally; it proves explicit route/env
propagation, not remote authentication. Linux tests explicitly skip without a user
manager; a skip is not topology evidence. No actual model is called by the suite.


### Hosted and local qualification coverage

The actual-path test module is marked `linux_only`: its `/proc`, Unix sockets,
fixed system tools and native-extension checks run on Linux, including the
staging push CI lane. `.github/workflows/ci.yaml` dispatches that lane through
`tests.yml`, whose hosted checkout is shallow. Candidate/native/readiness tests
remain enabled there. The retained historical `9ccb53e3` rollback case explicitly
skips only when that commit object is absent; it is mandatory local release
qualification with that object available (the existing final12 log records its
completed run). Do not interpret shallow-CI success as rollback qualification.
No workflow history expansion or test-side network fetch is performed. Existing
interpreters without a `pyvenv.cfg` use an empty manifest venv-config entry;
when present, that file remains pinned and prefix restoration remains asserted.
