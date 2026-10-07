# Running-worker progress warnings

The native dispatcher emits `running_progress_warning` when a launched local
worker has gone **900 seconds** without a new persisted progress receipt. The
15-minute threshold follows the semantic-progress warning policy; it is not a
runtime limit or proof of failure. The event records the run, PID, launch event,
progress episode, and elapsed time.

A warning leaves the worker, claim, holds, status, and retry policy alone.
Existing crash detection and explicitly configured runtime limits still apply
independently. Queued work and claims without a recorded spawn are not launches.
The observer requires a matching current run, assignment, claim, spawn PID, and
live local process.

For this bounded observer, progress means a newly registered Kanban attachment:
for example, an artifact, saved test output, or written handoff. The file must
be nonempty, regular, match its recorded size, and have a modification time at
or after this run's launch. Re-registering an existing attachment path does not
count. Its `attached` event is tied to the current run. Comments and heartbeat
notes alone do not count; neither do receipts from previous runs.

Attach intermediate evidence using the existing Kanban attachment interface.
Workspace edits and tests that have not been saved and attached are outside
this observer's visibility. File metadata establishes a persisted receipt, not
the quality, correctness, or authorship of its contents. Legitimate long work
can therefore receive a truthful warning.

The event ledger stores one warning per run/progress episode, atomically with
the dedup check. Repeated ticks, dispatcher restarts, and concurrent observers
cannot create another warning for that episode. A new qualifying receipt starts
a fresh 900-second interval. Running-task events are retained by native GC.

Existing ordinary notification subscriptions deliver the warning according to
their `notify`, `wake`, or `notify+wake` mode and routing policy. No subscription
or execution authority is created. Delivery uses existing cursor/retry behavior;
external exactly-once delivery is not guaranteed across a send/process-crash
boundary. Special autonomous-continuation subscriptions keep their separate
admission rules. Warning creation does not imply delivery if there is no
eligible connected return path.
