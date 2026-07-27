# Cross-profile cron event hooks

Hermes cron has two event layers:

1. `cron.hooks` — in-process callbacks inside the owning profile.
2. Atomic cross-profile event files — an opt-in, publish/subscribe interface.

Cron core is publisher-only. It never consumes, acknowledges, retries, deduplicates,
or deletes events. Those responsibilities live in the bundled
`cron-event-subscriber` plugin.

## Enable publishers

Each owning profile opts in independently:

```yaml
cron:
  events:
    enabled: true
    # Optional; empty means <Hermes root>/events/cron
    directory: ""
```

For isolated tests, `HERMES_CRON_EVENTS_ENABLED=1` enables publication and
`HERMES_CRON_EVENTS_DIR=/path` overrides the root.

Each event is written and fsynced to a temporary file in the destination
directory, then atomically renamed to:

```text
<Hermes root>/events/cron/pending/<source_profile>/<time>-<event_id>.json
```

A reader therefore sees either no event or one complete JSON object—never a
partial shared append. Temporary `.tmp` files are not events.

## Event schema and redaction

Each file contains one schema-version 1 object:

```json
{
  "schema_version": 1,
  "event_id": "hex uuid",
  "event_type": "create | update | remove | complete",
  "emitted_at": "2026-07-21T12:00:00Z",
  "source_profile": "writer",
  "job_id": "abc123def456",
  "job": {
    "id": "abc123def456",
    "name": "Morning digest",
    "schedule_display": "0 8 * * *",
    "enabled": true,
    "state": "scheduled"
  },
  "extra": {
    "success": true,
    "duration_seconds": 12.34,
    "error_present": false
  }
}
```

The publisher retains event IDs and profile ownership while excluding prompt
text and paths, scripts, origins/chat IDs, delivery targets, output content,
free-form errors, and conversation content. Allowlisted job fields are serialized
by expected scalar/list shape only; nested mappings and malformed values are
dropped. Skill and toolset names must remain plain identifier strings through
storage and publication; objects are never string-coerced. Explicit job names
must be strings. Schedule metadata is rebuilt from validated
`kind`/`minutes`/`expr`/`run_at` fields rather than copying arbitrary display
text. `complete` records
expose only the boolean `error_present`, not error text. Job names are included
only when the creator supplied an explicit display name; prompt-, path-, skill-,
or script-derived fallback labels are omitted.

## Enable and use the subscriber

Enable the bundled standalone plugin only in the profile that should consume
the shared stream (for example Ops):

```bash
hermes plugins enable cron-event-subscriber
```

The plugin is wired through existing plugin APIs:

- `/cron-events` drains pending events under a cross-process filesystem lock,
  returns their redacted JSON, and
  acknowledges each event only after its callback succeeds.
- `on_session_start` performs recovery and retention maintenance.

Durable state stays under the same event root:

```text
pending/<profile>/       atomically published, retryable events
processing/<profile>/    in-flight events; stale entries recover after a crash
acknowledged/<id>.json    successful acknowledgement and duplicate-ID marker
quarantine/<profile>/    malformed or ownership-mismatched records
```

Profile names `.` and `..` are rejected. On POSIX, each event-root directory
component is opened with `O_NOFOLLOW` and queue operations use pinned directory
file descriptors. The root and each selected profile/destination directory stay
pinned across listing, claim, callback, retry/quarantine, and acknowledgement, so
symlink or ordinary-directory swaps cannot redirect reads, writes, or renames.
Other platforms fail closed when an event-root component or queue file is a
symbolic link.

The subscriber holds one lock per event root across maintenance, callback
execution, and acknowledgement, so recovery cannot reclaim an active callback.
Callback failure moves the event back to `pending`. A duplicate `event_id`
whose acknowledgement exists is suppressed without invoking the callback.
Malformed records are quarantined. Recent temporary files are ignored; stale
temporary files, acknowledgements, and quarantine records are removed by the
subscriber's maintenance pass.

Optional plugin settings go in `config.yaml`:

```yaml
plugins:
  enabled:
    - cron-event-subscriber
  entries:
    cron-event-subscriber:
      claim_timeout_seconds: 300
      retention_days: 30
      temporary_retention_seconds: 3600
      max_events_per_drain: 100
```

Malformed settings fall back to these defaults. Numeric settings are bounded:
claim timeout to 0–86,400 seconds, retention to 0–3,650 days, temporary
retention to 0–604,800 seconds, and each drain to 1–10,000 events.

## Ownership boundary

Consumption is observe-only. A subscriber that detects a conflict must not edit
another profile's `cron/jobs.json`; it should create a Kanban request for the
`source_profile`, preserving that profile's normal mutation approvals.

## Publisher API

`cron.event_bus` exposes only publisher concerns:

- `build_cron_event(...)` builds a redacted record.
- `publish_cron_event(..., dry_run=True)` previews without writing.
- `publish_cron_event(...)` atomically commits one event file.
- `pending_directory_for_profile(profile)` resolves a publisher destination.

Normal cron mutations call `cron.hooks.emit(...)`, which publishes when enabled.
