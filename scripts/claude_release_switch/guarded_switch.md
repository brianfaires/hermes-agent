# Bounded source-switch correction — proposal for Ang adoption

This is an **offline implementation candidate**, not a deployment, installation,
service change, or acceptance of the held notifier feature. `guarded_switch.py`
is independent of the superseded `controller.py`; it does not import or revive
that framework. Existing task evidence and the original notifier commit are
untouched.

The incident's stop → successful source switch/import → model turn-cap exit
left no process responsible for start. One call to `guarded_switch.py run`
now performs arm → stop → exact switch → offline smoke → start → receipt
without another model call. A matching `recover` entrypoint is intended for the
**independent executor's transient service ExecStopPost**, outside the gateway's
own cgroup. The production guard creates no systemd service. The separately gated
qualification helper creates disposable transient toy units only. Retained evidence
covers eight real-systemd cases under standing approval; this offline documentation
and full-source qualification task does not rerun them or access a live manager.

## Frozen input and installation prerequisites

Adoption requires separately reviewed installation outside the switched checkout.
Keep the guard and smoke script in a private 0700 directory, both regular 0600
files. Keep the manifest in a private 0700 directory, as a regular 0600 file;
pass the externally frozen SHA256 of its exact bytes to both entrypoints.
The guard rejects symlink path components, hardlinked private files and noncanonical
paths. Manifest/install/state paths use only letters, digits, `_./-`; tracked source
filenames may contain spaces, quotes and Unicode and are always handled literally.
Use a canonical regular executable interpreter path, not a venv symlink. The
reviewed smoke script must arrange any required existing dependency paths itself
under `-I -S -B`; it must not install anything or rely on startup `.pth` execution.

The JSON manifest requires these fields (placeholders are not executable).
The optional `current` field is restricted to recovery-only binding below unless
`promotion` explicitly selects the separate promotion contract described later:

```json
{
  "repo": "/absolute/canonical/repository",
  "target": {"branch": "target", "sha": "40-lowercase-hex", "tree": "40-lowercase-hex"},
  "known_good": {"branch": "good", "sha": "40-lowercase-hex", "tree": "40-lowercase-hex"},
  "service": "exact-gateway.service",
  "baseline_invocation": "32-lowercase-hex-from-fresh-InvocationID",
  "executor": "unique-transient-executor.service",
  "state_dir": "/absolute/private/transaction-state",
  "interpreter": "/absolute/canonical/python",
  "smoke_argv": ["/absolute/canonical/python", "-I", "-S", "-B", "/absolute/private/smoke.py", "/absolute/canonical/repository"],
  "smoke_sha256": "64-lowercase-hex",
  "idle_observer": {"path": "/absolute/private/observe_idle.py", "sha256": "64-lowercase-hex", "home": "/absolute/fleet-home"}
}
```

Provision `state_dir` as 0700 and its empty `lock` as 0600 before invocation.
Use the **same state directory and lock** for forward and recovery. Keep the
single-use manifest/state together; never delete a journal to replay it. Every
new transaction needs fresh exact input binding and executor identity within the
standing approval's scope. An external exclusive hold must serialize all
writers/other transactions for this
repository and service, including transactions with different state directories.
The per-transaction lock is not a host-wide release scheduler.

The ordinary manifest may switch only between its two existing exact local branch tips.
In this frozen-tip mode there is no fetch, merge, push, branch advancement,
reset, clean, ref rewriting, package change, config restoration, or database restoration. Both tips must be
source-compatible with the installed dependencies, configuration and data; that
is an adoption prerequisite. Only regular tracked source files are
supported; submodules and tracked symlinks fail closed. Git filters (including
LFS), sparse/worktree configuration, source/index dirt and unknown branches are
unsupported. Ordinary Git EOL transformations (`text`, `text=auto`, `eol`, and
`core.autocrlf`) are supported, including tracked `.gitattributes` with
`*.ps1 text eol=crlf`. Verification hashes actual file bytes first. On mismatch,
Git hashes the actual file with its built-in normalization, without writing an
object or trusting cached clean status. Custom filter configuration is rejected
before this operation; non-EOL conversions (`filter`, `ident`, and
`working-tree-encoding` attributes) are refused on byte mismatch. Binary bytes,
normalized source content, index entries and executable modes must still match.
Untracked and ignored extras are preserved; a checkout collision
fails instead of overwriting them.

### Recovery-only binding

For an intentional source-only recovery, set `target` and `known_good` to
**identical branch/SHA/tree objects** for the approved known-good destination.
Different SHA/tree values for the same branch are refused. If the checkout is
already on that exact branch, no additional field is needed. If baseline source
is on another branch, add the optional manifest field:

```json
"current": {"branch": "baseline-source", "sha": "40-lowercase-hex", "tree": "40-lowercase-hex"}
```

`current` is allowed only when `target == known_good`, must name a different
existing local branch, and is checked with the same frozen ref/tree, regular-file,
index and byte rules. This explicitly binds the other permitted source; the guard
does not infer permission from an arbitrary clean HEAD. All refs remain frozen.
`baseline_invocation` still binds the running service and is independent of the
destination branch. No broader third-source forward-switch mode is supported.

Invoke `run` to arm and perform this recovery-only transaction. The initial
source must still pass preflight smoke before stop; this is not a bypass for
broken or dirty initial source. After stop, the guard switches to known-good,
verifies it, runs its smoke, verifies again, and only then starts. Success is
recorded/returned as `recovered` (exit **2**, including completed-hook readback),
never `switched`. The executor must preserve that outcome. `recover` alone still
requires the matching durable arm and fenced hook; without an arm it returns
`not_armed` and performs no source or service action.

## Explicit promotion mode (offline extension; parent owns release)

Omitting `promotion` keeps the frozen-tip contract. Promotion is a fresh second
transaction after an external live PASS on candidate C; startup is not that PASS.
Before its snapshot, the parent must preserve G on a dedicated branch and freeze
candidate C. `target` binds **main at C**, `known_good` binds the **preserved branch
at G**, and `current` binds the **candidate branch at C**. All three source objects
use the existing branch/SHA/tree schema. Initial HEAD must be `current`.
The additional manifest field is:

```json
"promotion": {
  "old_main": {"branch": "main", "sha": "G", "tree": "G-tree"},
  "live_pass": {"path": "/private/live-pass.json", "sha256": "receipt-SHA256"},
  "publication": {
    "remote": "origin",
    "url": "https://github.com/brianfaires/hermes-agent.git",
    "ref": "refs/heads/main",
    "old": "G",
    "new": "C",
    "tracking_ref": "refs/remotes/origin/main",
    "route": "brianfaires-gh"
  }
}
```

Replace symbolic hashes with full lowercase hex values; no release commit is
hardcoded. The private 0600 PASS receipt in a private 0700 directory outside the
checkout must have exactly these fields, with the candidate source object and
runtime/service identity matching this transaction:

```json
{
  "verdict": "PASS",
  "source": {"branch": "candidate", "sha": "C", "tree": "C-tree"},
  "repo": "/absolute/canonical/repository",
  "service": "exact-gateway.service",
  "invocation": "baseline-InvocationID",
  "interpreter": "/absolute/canonical/python"
}
```

The digest binds the external verdict, not a guard-generated health assertion.
The parent owns evidence collection, the actual running executable/invocation
binding, and authorization. Missing, changed or mismatched evidence fails before
stop. Recovery does not need the PASS file or an available remote.

Preflight verifies G→C ancestry, old main, candidate and preserved branch/tree,
clean exact source/index, origin URL, tracking mapping/tip, and actual remote tip.
`origin/main` must initially be G; actual remote main may be G or already C.
Any other remote value fails preflight. Only the usual origin wildcard fetch
mapping or its exact main-only equivalent is supported. Partial clones and
publication overrides (URL rewriting, credential/HTTP settings, includes,
push options, alternate origin push URLs/programs) are refused. No fetch occurs.

After durable arm and stop, the guard switches main at G and smokes it, writes
`ff_intent`, and runs `merge --ff-only` to the exact C. It verifies bytes/index,
writes `ff_complete`, and smokes C. Only the baseline ref map or the declared
main-at-C map is accepted across the FF journal boundary; the latter requires
durable FF intent. A completed FF cannot revert to the old map. The preserved
branch, candidate, every other ref, repository identity and configuration remain
bound. Partial checkout/dirt still fails closed.

The guard then journals the exact publication object with status `unknown`,
reads actual remote main, pushes the single non-force C:main refspec once only
if remote is G, and verifies remote is C. Remote already C skips the push.
No force, force-with-lease, fetch, retry, branch deletion or remote rollback is
used. Only after publication intent may origin/main advance G→C; a compare-and-swap
local `update-ref` reconciles it if Git did not. No other tracking ref may change.
Publication success and source verification precede start.

The publication route is deliberately fixed: origin's approved GitHub URL,
`HOME=/home/brian`, username `brianfaires`, and the existing
`/usr/bin/gh auth git-credential` helper scoped to https://github.com. Only the
publication read/push commands get that HOME/helper. Source verification retains
`HOME=/nonexistent` and disabled global/system Git config. The guard never reads,
copies or discovers auth files itself, accepts no command/environment callbacks,
and changes no configuration. A separate `disposable-local` route accepts only a
canonical `/tmp/...` bare repository for offline tests and uses no credentials.
That remote's existence is checked before arm, not as a recovery prerequisite.

After a publication failure/interruption, recovery first restores the preserved
G branch, smokes it, and starts it. Main remains C if FF happened; published
history is never rewound. Recovery then attempts **one readback, never a push**.
`publication_status` is `old`, `new`, `unrelated` (including missing ref), or
`unknown` when observation failed. An unknown result cannot grant replay or be
reported as unpublished. A `new` observation permits exact tracking reconciliation.
A lost remote cannot prevent source restoration/start. The completed receipt may
therefore describe recovered G with main C and unresolved publication. Later
history reconciliation belongs to the parent through a separately reviewed
non-force recovery/revert commit, not this transaction.

Existing original-invocation, timeout-to-fence, ambiguous-start, single-use,
smoke-before-start and fenced-hook rules apply unchanged. A publication timeout
returns `awaiting_fence`; the forward path does not reconcile while its executor
might still have descendants. Completed-hook checks do not repeat remote reads
or pushes. Death during recovery's post-start readback leaves the durable unknown
status; parent observation must reconcile it.

**External writer exclusion also covers remote main.** The non-force push has a
fresh pre-push read and a post-push read, not a server-side expected-old lease.
It cannot prove that another writer did not move an ancestral remote tip between
those reads. Parent must retain the approved exclusive publication window; this
extension does not create remote locking or authorize force-based publication.
Offline tests qualify disposable Git behavior, not the GitHub credential route,
actual service manager lifecycle, or release readiness.

## Proposed executor integration (not installed by this task)

After externally verified preflight, drain and writer hold, the independent
executor calls the copied guard **once**, passing `run MANIFEST --sha256 DIGEST`.
The script must run inside that executor's transient user-service cgroup.
The exact mandatory ExecStopPost argv is:

```text
INTERPRETER -I -S -B /outside/checkout/guarded_switch.py recover MANIFEST --sha256 DIGEST
```

Paths must be the frozen canonical paths; no shell, wrapper, ignored error prefix
or extra stop-post commands. The transient executor must have `KillMode=control-group`,
`SendSIGKILL=yes`, `Restart=no`, `Type=exec` or `simple`, `ExitType=main`,
`RemainAfterExit=no`, `FinalKillSignal=SIGKILL` (observed as `9`), and
`TimeoutStopFailureMode=terminate`. Actual `RuntimeMaxUSec` must be positive and
at most 900 seconds, with `RuntimeRandomizedExtraUSec=0`. Actual
`TimeoutStopUSec` must be 300–600 seconds inclusive. Missing, infinite, invalid,
or out-of-range properties are refused, including at a second executor check
immediately before the durable arm, after preflight smoke and source checks.

The independent Claude/Codex parent may remain MainPID; the guard is allowed to
be its child. On `awaiting_fence`, the parent MUST exit immediately: no readbacks, reporting,
or further tool/model calls. The transaction journal is durable evidence; reports
belong after fenced recovery. For other failures the parent should propagate
failure and exit promptly, but
a surviving parent cannot postpone termination indefinitely: the finite runtime
forces shutdown and group kill escalation before the hook. The 5–10 minute stop
budget also gives the hook time for known-good smoke (up to 60 seconds), service
stop (up to 120 seconds) and start (up to 60 seconds), checkout and source verification. This is a
finite allowance, not a proof that arbitrary repository sizes or slow storage
fit: adoption must qualify the actual recovery path within that allowance.
A parent ignoring termination may consume one stop timeout before final kill;
the stop-post command has its own stop timeout. Runtime expiry is measured from
executor activation, not from arm. A late arm may therefore be interrupted
immediately and rely on the already durable journal. Do not catch a failed guard and return to
model-driven release steps. No permanent watcher or gateway definition change is
needed. Systemd setup, permissions, real cgroup behavior, drain, live readiness,
and the lifecycle of the independent model executor remain adoption work.

Before arming, the adapter reads the actual executor unit and checks its transient
status, invocation against systemd's `INVOCATION_ID`, cgroup membership, kill policy,
and exact mandatory hook. Recovery additionally requires `SubState=stop-post`,
`MainPID=0`, `ControlPID` equal to itself, and no other process anywhere in the
executor cgroup hierarchy. A model assertion or free lock is **not** fencing proof.
Neither CLI accepts a fixture adapter or alternate service command; tests inject
an adapter through the Python API only.

## Guarantees and deliberate failure behavior

- Preflight failure leaves repository, service and transaction journal unchanged.
  Recovery with no arm is a no-op, including model exit before the call.
- The arm is written before stop with atomic replacement, 0600 permissions, file
  fsync and directory fsync. Stop failure or death before a later phase write
  still leaves a recoverable arm. Run and hook share a nonblocking file lock.
- Recovery binds the exact manifest, executor invocation, repository device/inode
  identity, Git configuration and all local refs. It checks HEAD, index entries,
  executable modes and actual tracked blob bytes, bypassing stat-cache and
  assume-unchanged shortcuts. It never cleans up unknown/dirtied source.
- A synchronous target failure gets one known-good fallback. Recovery stops only
  the original baseline invocation if still active, switches the unchanged
  known-good branch, verifies exact source, runs smoke, verifies source again,
  then starts. Failed fallback becomes `recovery_required`, never an endless retry.
  A stop command that raises after reaching inactive/failed with PID zero can
  recover from the durable arm. A partially stopped service still deactivating
  with a live PID fails closed as `recovery_required`; no source switch or start
  follows that observation.
- Ordinary commands have a 60-second timeout. Only the shared Systemd stop
  adapter gets 120 seconds, in both forward and recovery paths. Before arming,
  the actual gateway `TimeoutStopUSec` must be finite, positive and at most
  90 seconds; unknown/infinite/unsupported values refuse before any arm. Forward timeout records `awaiting_fence` and
  returns nonzero: descendants may survive the direct child's termination, so
  only the later fenced hook may recover. A surviving model parent is bounded by
  the checked executor runtime; no prompt promise or extra model turns are needed.
  Hook failures stop with explicit error.
- New/unrelated gateway invocations are never stopped. A crash after start but
  before its completed receipt is intentionally ambiguous: the hook refuses
  further service action, even if the new process might be this transaction's.
  An operator must reconcile that case. This trades automatic recovery coverage
  for protection of new work.
- A completed receipt is rechecked against actual exact source and active gateway
  invocation. A missing or misleading model `result.md` is irrelevant. No-op
  verification does not run a second start. Replayed forward calls are refused.
- Durable state is updated to `recovery_required` on recovery failure when the
  validated lock and manifest permit writing. Invalid/stale manifests, failed
  fencing, corrupt state and lock contention instead return an explicit error
  without overwriting another transaction's state.

Stdout distinguishes `switched`, `recovered`, `not_armed`, `deferred_busy_or_unknown`,
and `awaiting_fence`;
errors are JSON on stderr. Exit 0 means switched or not armed; 2 means known-good
recovery succeeded; 1 means refused or recovery still required. Every CLI result
has `deployment_accepted: false`. Service `active`/MainPID/InvocationID readback is
transaction-level startup verification, **not application health or feature acceptance**.

## Limits and remaining qualification

The smoke script is pinned, trusted offline code, not a sandbox. It must validate
imports and the required configuration/dependency compatibility without network,
credentials, writes to live state, background children, or side effects. It gets
a disposable HOME/HERMES_HOME and a minimal environment; the generic guard cannot
prove that a script actually implements adequate checks. Production config
validation must be reviewed explicitly rather than inferred from a fixture import.
The deployed guard, interpreter, dependencies, config and state must remain under
the external writer hold. Owner-private paths and hashes do not defend against
malicious root or another malicious process with the same UID; filesystem/service
checks are not atomic against independent actors ignoring that hold.

This does not recover data/schema changes. It cannot promise recovery after host
power loss, storage/fsync failure, kernel/systemd failure, an unavailable user
manager, killing the hook itself, or loss of the entire machine. There is no boot
recovery daemon. A mid-checkout crash may leave dirt and require manual recovery.
Retained eight-case qualification exercised a real user service manager with
disposable toy units. It is historical lifecycle evidence, not a fresh binding to
a future production repository, runtime, configuration, service or executor.
Before a future arm under standing approval, bind those exact inputs, qualify
source/dependency/config compatibility and recovery timing, and verify the live
executor lifecycle, writer hold and drain. Offline fake-service results do not
establish production readiness or authorize promotion of incident source.

## Offline validation

`tests/scripts/test_guarded_switch.py` is a standalone stdlib unittest suite using
real disposable Git repositories, real subprocess exits/SIGKILL, a real offline
Python import, and a persistent fake service adapter. It never invokes systemctl,
uses no Hermes imports, and isolates HOME/HERMES_HOME for all child processes.
Run the offline files only through the canonical runner:

```text
scripts/run_tests.sh tests/scripts/test_guarded_switch.py tests/scripts/test_guarded_switch_harness_safety.py tests/scripts/test_guarded_switch_systemd.py tests/scripts/test_guarded_switch_promotion.py tests/scripts/test_guarded_switch_observers.py -q
```

The integration candidate includes: this document, `guarded_switch.py`,
the three existing test files above, and `test_guarded_switch_promotion.py`. Include
`test_guarded_switch_harness_safety.py`, in integration and review; it is part
of the manager-isolation boundary even when still untracked in a source worktree.

Use a disposable HOME and the authorized canonical interpreter via the runner's
existing `HERMES_PYTHON` selection. Do not bootstrap a missing test dependency.
Regressions include real `.gitattributes` CRLF checkout and `core.autocrlf`,
content dirt hidden by assume-unchanged, binary byte changes, recovery-only from
both the destination and an explicitly bound other branch, partial stop failures,
and successful known-good smoke observed before startup.
Under pytest, only the external-SIGKILL test uses the repository's documented
`live_system_guard_bypass` marker: it signals its own disposable `Popen` child,
which the guard cannot recognize when the test interpreter lacks `psutil`.
Standalone execution still needs only the standard library.
No existing controller tests (which can exercise systemd) are needed or run.


## Retained real lifecycle qualification and future exact binding

Ordinary pytest collection/execution always skips all real-systemd cases before
any probe. Direct file selection does not opt in. The standalone CLI requires
`--allow-disposable-user-systemd` and one explicit `--case`; missing opt-in fails
before even creating its fixture. Invocation form for a future explicitly selected
disposable case within standing approval (not part of this offline task):

```text
CANONICAL_PYTHON tests/scripts/test_guarded_switch_systemd.py --allow-disposable-user-systemd --case command_timeout_parent
```

The helper uses UUID-owned `guarded-switch-test-<32 hex>-{app,executor}.service`
transient units and validates exact ownership for manager actions. No unit files,
manager-path writes, reload/reexec, global reset, enumeration or broad kills exist.
The toy gateway uses `RemainAfterExit=yes`; the executor's `Wants` reference keeps
its definition referenced across explicit stop/start. Cleanup stops and resets
only the two owned names. The guard's hook remains the copied actual script path.
Executor runtime is 90 seconds, stop budget 300 seconds, observation wait at most
420 seconds, each cleanup stop at most 660 seconds. Manager failure can still
prevent cleanup; this helper makes no machine-loss guarantee.

Cases cover unguarded turn-cap negative control, exit before arm, SIGKILL after
switch, target import failure, missing model report with nonzero parent, surviving
parent after guard kill, real 60-second command timeout with surviving parent,
and model failure after stop. Surviving-parent cases observe the nonzero child
return, live MainPID and stopped gateway before waiting for runtime-driven fencing;
they never manually stop the executor to manufacture that recovery.

The separate offline harness-safety tests inject deny/recording adapters to check
import, default entrypoints, opt-in dispatch and exact-unit action restrictions.
Those offline tests do not qualify actual systemd lifecycle semantics; the retained
eight real-systemd cases provide that bounded toy-unit evidence. Parent review and
fresh exact deployment binding remain necessary under standing approval.
Historical qualification is not a current production readiness check.


## Fresh idle observation before arm

The Systemd adapter requires a pinned `idle_observer` binding. Install the reviewed
`observe_idle.py` outside the checkout in a private directory and bind its exact
hash and canonical fleet home. There is no caller-selected command or callback.
The guard passes the just-verified current source SHA and gateway PID; the old
run122 PID/SHA binding is not reused. Missing binding refuses a new forward run;
recovery of an already armed legacy manifest remains possible.

After source/smoke/promotion preflight, executor and gateway verification, the
adapter checks the actual stop bound and runs this observation immediately before
the durable arm. It reuses the existing control socket, all-profile cron claims
and execution stores, board tasks/open runs, source cwd consumers and external
drain marker. Busy/unanswered/malformed observation returns
`deferred_busy_or_unknown` (exit 1), leaving the gateway running and journal absent.
Missing roots or unreadable stores are unknown. The script samples gateway
activity last, requiring the exact PID/source and a sample at most five seconds
old. Filesystem/process scanning retains the previous observer's visibility limits.
This narrows the stale observation gap; it is not atomic admission, a reservation,
or a lock/drain framework. Keep the existing external writer hold and restrictions.

## Disposable terminal-owner observer: proposed wiring only

`terminal_observer.py` only observes the exact executor unit/invocation and the
bound transaction journal. MainPID OR ControlPID OR an active/transitioning unit
keeps it silent: a live ExecStopPost is not terminal. Missing/unloaded/replaced
units are owned observation failures, never a terminal release success. Complete
journals distinguish switched/recovered; incomplete terminal journals request
recovery attention. No journal after exact terminal means not armed. No outcome
confers release acceptance, retries release work, or mutates a TEST.

A fresh private JSON binding has exactly these fields:

- `executor`, `invocation`: exact future unit and its actual invocation, never the
  old run122 identity. Keep the transient definition referenced through observation.
- `manifest_sha256`, `journal`: frozen transaction digest and absolute journal path.
- `owner`: explicit controller attention destination label; routing is separately
  pinned in the cron job's existing `deliver`/`origin` fields.
- `state_dir`: separate canonical 0700 directory with preprovisioned 0600 `lock`.
- `expires_at`: absolute Unix epoch deadline, normally arm time plus 20 minutes.
- `max_attempts`: 3 (allowed 2–5); `retry_seconds`: 60 (allowed 30–300).

The binding itself is regular private 0600. Pass its SHA256 on every invocation.
Owner-controlled files, script/interpreter and the wrapper below are part of the
reviewed frozen packet, under the same external writer hold. No self-installation.

For a future controller-authorized schedule, put a narrowly bound `.sh` wrapper
under that scheduler profile's `HERMES_HOME/scripts/`, using literal canonical
paths and `exec CANONICAL_PYTHON -I -S -B /private/terminal_observer.py
/private/binding.json --sha256 EXACT_DIGEST`. The existing scheduler only accepts
scripts under that scripts directory and does not pass script arguments, hence
the wrapper. Use `no_agent=true`, `schedule="every 1m"`, `repeat=30`, explicit
controller delivery routing, and a working directory outside the source checkout.
Bind the actual executor invocation before enabling this fresh schedule. A silent
run still consumes a scheduler repeat, so a single interrupted invocation must
not be the only scheduled opportunity. Do not revive the expired old one-shot.
These are proposed values and instructions, not a job created by this change.

Stdout is merely a request for the scheduler to deliver. The script persists an
attempt before stdout and retries the same token at most three times with at least
60 seconds between attempts. It never records delivery success itself. The owner
must receive the notice, deduplicate its stable token, and explicitly run the same
bound script with `--ack TOKEN --receipt RECEIPT_REFERENCE`. That records actual
owner receipt in `observer.json`; it does not acknowledge release correctness.
Do not use cron `last_status=success`, a script attempt, or even adapter-level
`delivery_outcome=delivered` as an owner acknowledgment. Only the owner may supply
this receipt. Acknowledged invocations remain silent. The first notice is frozen;
if it is an observation failure, the owner must investigate rather than treating
it as a later terminal outcome. A failure receipt NEVER authorizes replacement or
takeover. Before any takeover, the controller must freshly verify exact executor
AND recovery-hook terminality; live or unverified ownership prohibits takeover.
Acknowledging or investigating a read/identity failure is not that verification.

For an unacknowledged observer, expiry is persisted independently of stdout:
`expired=true` and `expiry_observed_at` record the deadline crossing even while
the original owner remains live or the attempt budget is exhausted. The
`expiry_blocker` distinguishes `owner_live`, `terminality_unverified`, and
`unacknowledged_terminal`. A live owner still produces no stdout and consumes no
additional emission attempt. Notice kind/token stay frozen; expiry bookkeeping
never supplies a receipt or a terminal release verdict. If attempts remain,
terminal expiry can emit an expiry notice; unknown identity can emit an
expiry/unknown notice. The final emission attempt surfaces
`unacknowledged_exhausted`; exhausted runs never emit again.

The absence of an exact owner receipt in the bound `observer.json` is an
AUTHORITATIVE blocker regardless of the cron job's final success/completed state,
latest error, or delivery outcome. Missing/unreadable observer state is also a
blocker, never evidence of delivery. An acknowledged observation failure remains
an owned investigation requiring fresh exact executor and hook reconciliation.

The controller must own an independent fixed end-of-window review (31 minutes in
this proposal), with a named owner/destination, accepted review responsibility,
receipt location, and escalation for missing state/receipt, expiry, exhaustion or
acknowledged failure awaiting reconciliation. The parent coordinates that actual
ownership separately; this source packet does not instantiate it. This deadline
must not depend solely on receiving the observer notice or assume cron completed:
`every 1m, repeat=30` is finite opportunities, not a hard 30-minute lifetime;
downtime and completion-relative scheduling can extend it. No job is created here.

The scheduler counts failed/interrupted recurring runs toward the repeat budget.
Its job-level `last_delivery_error` describes only the latest run: a later
successful silent tick passes `delivery_error=None`, clears an earlier delivery
error and resets the failure streak. Thus final cron success and a cleared error
can coexist with every notice delivery having failed. Investigate earlier attempts
using per-execution history and saved output, plus retained delivery monitoring
where available, not just the latest job record. The execution ledger retains
final responses but does not store `delivery_outcome` as a column; that outcome
is passed to monitoring, whose retention must not be assumed. Missing or pruned
history cannot imply delivery, and output proves only what was proposed for
sending. Even retained delivery evidence cannot replace the exact owner's receipt.
A process killed after persistence but before stdout can lose one attempt; later
scheduled observations retain their opportunities. A send followed by a crash
can duplicate a token before owner acknowledgment. There is no exactly-once
claim. The existing scheduler has no script-to-owner acknowledgment channel and
cannot guarantee a final alert if all remaining runs/deliveries are interrupted,
the gateway stays down, or the job budget expires while the hook remains live.
The explicit owner receipt and final manual review address that boundary without
global cron changes; if unattended guaranteed escalation is required, this scheme
cannot provide it. No live delivery or outage guarantee was tested here.
