# Cross-profile cron event hooks

Hermes cron has two event layers:

1. `cron.hooks` — in-process Python callbacks inside the owning profile.
2. Cross-profile JSONL event streams — an opt-in publish/observe interface for
   other profiles such as Ops.

The cross-profile stream is intentionally observe-only. A profile that sees a
cron conflict in another profile must not edit that profile's `cron/jobs.json`.
It should file a Kanban work order against the owning profile and let that
profile perform any schedule mutation under its normal approval gates.

## Enable publication in an owning profile

Publication is disabled by default. Each profile that wants to expose redacted
cron lifecycle events must opt in:

```yaml
cron:
  events:
    enabled: true
    # Optional. Empty/default means <Hermes root>/events/cron
    directory: ""
```

For tests or one-off dry runs, `HERMES_CRON_EVENTS_ENABLED=1` enables the
publisher for the current process and `HERMES_CRON_EVENTS_DIR=/path` overrides
the output directory.

## Event path

Default path:

```text
<Hermes root>/events/cron/<source_profile>.jsonl
```

Examples:

```text
~/.hermes/events/cron/default.jsonl
~/.hermes/events/cron/ops.jsonl
~/.hermes/events/cron/writer.jsonl
```

The source profile is inferred from `HERMES_HOME`: `<root>/profiles/<name>`
emits as `<name>`, and the root profile emits as `default`.

## Event schema

Each line is one JSON object:

```json
{
  "schema_version": 1,
  "event_id": "hex uuid",
  "event_type": "create | update | remove | complete",
  "emitted_at": "2026-06-27T20:30:00.000000Z",
  "source_profile": "writer",
  "job_id": "abc123def456",
  "job": {
    "id": "abc123def456",
    "name": "Morning digest",
    "schedule_display": "0 8 * * *",
    "schedule": {"kind": "cron", "expr": "0 8 * * *", "display": "0 8 * * *"},
    "next_run_at": "2026-06-28T08:00:00+00:00",
    "last_run_at": null,
    "last_status": null,
    "enabled": true,
    "state": "scheduled",
    "repeat": {"times": null, "completed": 0},
    "skills": ["morning-brief-automation"],
    "no_agent": false,
    "enabled_toolsets": ["web"]
  },
  "extra": {
    "success": true,
    "duration_seconds": 12.34,
    "error_present": false
  }
}
```

`extra` is present only for fields supplied by the emitting hook, mainly
`complete` run metadata. Free-form error text is not serialized across profile
boundaries; consumers get only `error_present`.

The shared stream deliberately does not include prompt text, prompt paths,
script paths, origin chat metadata, delivery targets, saved output, free-form
error strings, or full conversation content. It is suitable for calendar and
overlap analysis, not for reconstructing what the cron job said or did.

## Requesting a schedule change

Consumers should treat the stream as telemetry. If Ops detects a schedule
overlap, the request interface is a Kanban work order to the owning profile:

```text
Title: Reschedule cron <job_id> to avoid overlap
Assignee: <source_profile>
Body:
  Observed from cron event stream: <event_id/path>
  Current schedule: <schedule_display>
  Conflict: <other job/profile/time window>
  Requested change: <proposed schedule or constraints>
  Safety: owning profile must dry-run/read back cron state, preserve approval
          gates for cron mutation, and verify with cron list after any change.
```

This preserves cross-profile boundaries: Ops coordinates and requests; the
owning profile mutates its own cron schedule.

## Programmatic helpers

`cron.event_bus` exposes:

- `publish_cron_event(event_type, job=..., dry_run=True)` — build the exact
  event record without writing it.
- `iter_events(profiles=[...])` — read one or more profile JSONL streams.
- `event_file_for_profile(profile)` — resolve the stream path.

Normal cron mutations call `cron.hooks.emit(...)`, which publishes events when
publication is enabled.
