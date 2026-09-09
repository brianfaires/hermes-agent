# Private journal — FC-42 candidate

Disabled by default. Source/configuration ownership passes to **Ang release task
`t_ddd2e9dc` / Ops**. Nothing here activates a plugin, cron job, model, or bank.
Historical capture/rendering and batch-recovery code adapted from Brian Faires /
Ang commit `a485633e0b7dfaa3fadf8bb438d538dbac5d7e6b`; core dispatch is a current-API
rewrite. FC-41 `/new` is retained; this is not the FC-43 global memory policy.

`/log <text>` is a real plugin command. It removes exactly one whitespace command
delimiter. Empty/whitespace input writes nothing. Successful acknowledgement is
ID and capture time only. Capture calls no model/memory/session APIs. CLI Enter
and editor submit, TUI RPC, and gateway cold/busy dispatch take the private path
before ordinary hooks, input history, queues and session creation. Literal prompts
with gateway control disabled remain literal. Bang commands keep their existing
shell/export behavior. Text capture only: platform-split continuations, voice,
attachments, quoted/replied-to messages and imported history are not new capture
surfaces. Use one delivered command per record. Desktop renderer is not a
qualified capture surface (backend RPC supports it, but desktop input history is
separate). Use CLI, TUI, or authorized Telegram/Discord direct messages for rollout.

## Installation packet (not executed)

1. Ship the reviewed source and the generic command seam together. Plugin code is
   in `plugins/private_journal/`; there are no model tools or lifecycle hooks.
2. Merge `installation/config.example.yaml` into one explicit profile. Choose a
   dedicated vault, external schema root, inexpensive **OpenRouter** model and
   profile-specific Hindsight bank/URL. Existing credential resolution supplies
   secrets. There is no paid fallback, subscription lane, auto-install or auth
   repair. Do not enable ordinary Hindsight auto-retain for processing.
3. Set `plugins.enabled` to include `private-journal` and `batch_enabled: true`
   only during parent-owned activation. Supply the three external schema files:
   `personal-history-log.md`, `data-dictionary.md`, `templates/entry-template.md`.
   Their content is not shipped here. Brian's explicit finalized-entry ingestion
   decision supersedes the historical schema's blanket memory prohibition.
4. Render `installation/private-journal-midnight.py.example` under the selected
   profile's `scripts/`. The existing scheduler only executes scripts there.
   Use the JSON packet as arguments to existing `cron.jobs.create_job` in that
   profile. Cron expression **`0 0 * * *`**, profile/scheduler local timezone;
   verify its configured timezone, including DST, during activation. One job,
   `no_agent=true`, no session attachment, no backfill.
5. Keep delivery `local`: script stderr emits at most two sanitized lines once
   per unresolved failure incident, success/empty stdout is silent, failures exit
   nonzero. Ops can collect this local alert. Do not enable generic remote cron
   failure delivery expecting this dedupe: upstream independently adds recurring
   failure summaries/streak nudges. No new notification service is installed.

## Durability and retention

POSIX private directories/files use 0700/0600. Descriptor-relative traversal,
no-follow opens, exclusive temporary files, hard-link publication and file +
directory fsync prevent symlink substitution or overwriting an immutable record.
The trust boundary is the owning OS account; another process with that account's
permissions can modify plaintext. Filesystems must support these POSIX semantics.
No names or narrative appear in filenames.

Holding JSON lives under `<profile>/journal/holding/`. One fresh bounded model
request covers **all** pending records (100 records / 96,000 prompt bytes maximum;
4,096 output tokens / 120 second SDK timeout). Oversize rejects before the call;
no silent subset. Strict JSON/ID/field/basis validation precedes deterministic
Markdown rendering. Logged time never substitutes for event time. Raw UTF-8 is
recoverable exactly with a length/hash-delimited section; claims, quotes, unknowns,
corrections, caregiving, diet, sleep, substance and work/incidents retain their labels.
Diet entries store only stated foods/drinks and stated amounts/units, with unknowns
left null and no nutrition or medical interpretation. Sleep entries preserve the
existing fields and may label stated naps with `kind: "nap"`; wake-ups stay in
`interruptions`.
Structural checks cannot prove the model's factual fidelity; journal review and
append-only correction entries remain necessary.

A durable extraction manifest permits retry without another model call after
staging. Final paths are `entries/YYYY/MM/YYYY-MM-DD/HHMMSS-<capture-id>.md`.
Files and per-entry receipts may exist during publication, but are **provisional**
until their single batch completion marker commits the entire batch. Consumers
must honor that marker. Before commit every raw JSON is copied to a verified
archive. Originals remain in holding by default. Crash before manifest fsync can
require another model call; crash after manifest staging replays publications,
never overwrites finals. There is no multi-file atomic visibility claim.

`--cleanup` is dry run. Only `--cleanup --delete-holding-duplicates` permits deleting
holding duplicates after final/hash/receipt/batch/archive verification. No interval
is selected. No archive/final/manifest/receipt pruning exists. Failed or pending
records stay. A cleanup retry reads archived originals for completed manifests.
Do not schedule or run production cleanup as part of activation.

## Intentional finalized-entry Hindsight ingestion

Only committed FINALIZED entry documents are retained. The full authoritative
entry (raw section plus labeled extraction in a **single** document) is sent once;
no separate raw retain, extraction retain or processing-chat retain. Metadata
carries stable entry/source/profile identity, capture date and final hash. Context
explicitly distinguishes capture/event time and attribution/uncertainty.

The existing `hindsight_client.Hindsight.retain` accepts `bank_id`, `document_id`,
`timestamp`, `context`, `metadata`, and `retain_async=False`; its documented
`update_mode` supports replace/append. These group/replace semantics do **not**
prove transaction-level exactly-once delivery. This implementation does not retry
an unresolved intent. It fsyncs an identity/hash-bound intent before the real SDK
call and writes a receipt only after explicit synchronous success. Timeout,
negative/async result, or remote-success/local-receipt crash leaves an uncertain
intent. Journal completion is preserved and future journal batches can continue.
Changing bank/profile/content with an existing receipt fails closed.

For an uncertain entry, Ops must inspect that exact bank/document and any pending
remote operation. Then explicitly run the script with `--reconcile-entry <id>` and
`--confirmed-remote-outcome present` (certify matching stored final) or `absent`
(certify no stored document and no operation still capable of succeeding). Absent
archives the old intent before permitting one new attempt. Never use absent merely
because a request timed out. No automatic remote search/backfill is performed.

Rollback: disable the profile plugin and pause its midnight job. Retain all data.
Also keep journal channels excluded from history/backfill when disabled; disabling
a plugin removes its runtime metadata and cannot retract messages at the provider.
