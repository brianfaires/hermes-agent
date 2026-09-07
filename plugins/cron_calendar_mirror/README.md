# FC-11 candidate: cron Calendar mirror

This optional fork integration mirrors forecasts and actual run results without
participating in cron dispatch. Reconciliation is a deterministic Python command:
no model calls, agent turns, hooks, cron provider, or background thread. Nothing
in this candidate enables the plugin or installs a periodic invocation.

## Data and behavior

- Forecasts use `next_run_at`, cron's own timezone/schedule calculation, and a
  finite horizon. Defaults: seven days, at most 32 occurrences per job. Jobs
  with unlimited repeats and cadence below 60 minutes get daily summaries.
  Finite jobs get concrete occurrences up to their remaining repeat budget.
  These are forecasts, not promises of dispatch: catch-up/recovery, run duration,
  pause/resume, and interval rearming can change the next scheduled instant.
  Stale interval projection preserves phase; it does not manufacture missed runs.
- Actual execution events use a profile-path fingerprint plus the exact execution
  ID, independently of forecast IDs. Descriptions contain status, timestamps,
  scheduled instant, job name/schedule snapshot, and the **final response**.
  No output-file timestamp matching or latest-result overwrite is used.
- `cron/executions.py` adds `execution_results` within the existing profile-local
  `cron/executions.db`. The shared scheduler captures a minimal snapshot at the
  running transition and the final response at its existing owner-checked terminal
  boundary. Stale-owner/shutdown branches retain their failure semantics and do
  not publish discarded responses. Prompts, full transcripts and tool logs are
  excluded. Existing execution query payloads and monitoring events remain
  unchanged; only the explicit retained-result iterator exposes response content.
- Result rows survive the normal 1,000-status-row pruning and removed one-shots.
  All retained terminal results are read in pages; there is **no total scan cap or
  age cutoff**. More than 500 runs between scans cannot silently lose answers.
  Failed writes have no local success cursor to advance. Subsequent scans retry
  stable IDs and verify readback. Sync time/memory and retained storage grow with
  history: pagination bounds database reads, not total reconciliation memory or
  Calendar calls. Size the periodic interval for the profile's actual history.
- Successful responses are redacted before export. Calendar descriptions show at
  most 6,000 response characters, with an explicit truncation notice; the local
  ledger keeps the complete final response. Failure detail is optional and bounded
  to 400 redacted characters. This is secret-pattern redaction, not a PII filter.
- Paused, removed, rescheduled, or out-of-horizon forecasts are archived in place
  (gray with an explanatory description). Resume restores the same live IDs.
  Run history is never archived or deleted. Updates preserve user-edited titles.
  This command has **no Calendar delete operation**. Externally deleted/tombstoned
  IDs are reported as failures; the command does not invent replacement IDs.

## Privacy and prerequisites

The target must be an explicitly configured secondary calendar ID ending in
`@group.calendar.google.com`, with CalendarList ownership evidence and exactly
one ACL rule: the explicitly approved owner's user identity with role `owner`.
Aliases, primary calendars, writer-only access, additional user/group/domain/
public ACL entries, malformed ACLs, and unreadable ACLs fail closed. The worker
revalidates routing and the complete paginated ACL for each operation; it never
creates calendars, modifies ACLs, invites guests, or requests new OAuth scopes.

Existing events must already be private, guest-free, nonrecurring, and match
our profile/job/execution-or-occurrence identity. Even an event marked private
with existing guests is denied. Before each patch the worker rereads the event
and sends `If-Match` with its ETag, so concurrent guest/visibility changes reject
the patch. Writes suppress notifications and reminders and mark time as free.

Access reuses the profile's installed
`skills/productivity/google-workspace/scripts/google_api.py` and its
`build_service("calendar", "v3")`. The inspected profile implementation uses
`HERMES_HOME/google_token.json`; the token must already have explicit granted
scope metadata and sufficient existing permissions for CalendarList, ACL reads,
and event writes. Missing permissions mean no writes, with no scope repair or
OAuth setup. An app-created-only grant may be insufficient. The command never
prints tokens or result contents in error reports. The skill's normal refresh
path may refresh an existing credential when an operator later runs live sync.

ACL checks and event writes cannot be one atomic Google transaction. ACL changes
after a check (or after export) can expose previously stored data; only the owner
should administer this dedicated private calendar. Do not use a managed/shared
calendar with broader organizational visibility policies. Live policy and granted
permission verification remain Ops UAT; this candidate made no live API calls.

## Activation packet (Ops/parent only; not executed)

Integrate the cohesive commit into the approved fork runtime first. The source
package currently resides in this fork's `plugins/cron_calendar_mirror`; invoke
it with that runtime's Python and checkout on `PYTHONPATH`. Keep the canonical
runtime launcher unchanged. Do not copy only the plugin without the generic
ledger extension: historical final responses cannot be reconstructed.

Set these **non-secret** settings in the intended profile's `config.yaml`, after
confirming the target and sole owner. Placeholder values below are not approval
of a real target:

```yaml
plugins:
  entries:
    cron_calendar_mirror:
      settings:
        calendar_id: "APPROVED_ID@group.calendar.google.com"
        approved_owner: "APPROVED_OWNER@example.test"
        horizon_days: 7             # 1..31
        max_occurrences_per_job: 32 # 1..200; forecast cap, not a result cap
        high_frequency_minutes: 60 # unlimited jobs below this use daily summaries
        result_page_size: 200      # 1..500; ALL result pages are read
        include_error_detail: true
        # worker_python: /approved/python-with-existing-google-libraries
```

From the integrated checkout, with `FC11_PYTHON` set to its approved absolute
interpreter path and `HERMES_HOME` explicitly set to the approved profile:

```bash
PYTHONPATH="$PWD" "$FC11_PYTHON" -m plugins.cron_calendar_mirror status
PYTHONPATH="$PWD" "$FC11_PYTHON" -m plugins.cron_calendar_mirror sync --dry-run --json
PYTHONPATH="$PWD" "$FC11_PYTHON" -m plugins.cron_calendar_mirror sync --json
```

`status` performs no Calendar calls; file presence is not proof of authorization.
Dry-run reads Calendar/ACLs but writes no events. Sync exits 0 on success, 1 on
per-event failures, and 2 when unavailable/unconfigured. No cron job is changed.
The optional plugin slash command is `/cron-calendar-mirror`; enabling it is not
required for the module command or periodic reconciliation.

After approval/UAT, arrange an existing OS scheduler/timer to invoke that exact
module command periodically (for example every five minutes), with explicit
checkout working directory, interpreter and `HERMES_HOME`. Serialize invocations
using the scheduler's existing non-overlap facility. Do not schedule an LLM cron
prompt to reconcile. No timer/unit/job was created by this candidate.

## UAT handoff

Retained automated tests use temporary profiles, the real cron store/ledger and
scheduler, a fake `run_job`, and an executable fake Calendar boundary. They cover
create/update/pause/resume/remove, one-shot removal before completion, separate
run IDs, full final-answer persistence, >500 tied-timestamp records, bounded
paging, retries/readback, ACL drift, private events with guests, ETag rejection,
missing grants, intervals, finite budgets, and DST against scheduler behavior.

Ops should separately approve and exercise a disposable private Calendar target:

1. Verify its identity, ownership, ACL and preexisting granted permissions.
2. Dry-run; inspect planned forecast counts/caps and the correct profile.
3. Run disposable one-shot and recurring jobs with non-sensitive fixture answers;
   sync twice and verify results land on their execution IDs with no duplicates.
4. Pause/resume/update/remove; inspect gray archived forecasts and retained runs.
5. Exercise a controlled outage/backfill and confirm every retained fixture result
   appears. Confirm an edited title survives and no invitations are emitted.
6. Keep destructive, guest/ACL-drift and tombstone cases in the fake tests; do not
   alter live sharing or delete Calendar history to test failure handling.

No live UAT or legacy history migration is claimed. Runs completed before this
candidate only have whatever status/error rows remain in the old bounded ledger;
old final responses are unavailable. Moving a profile directory changes its
fingerprint; coordinate that separately to avoid a second set of event IDs.

## Rollback

Stop only the externally arranged mirror timer and disable the optional plugin
command if enabled. Leave existing Calendar history and the execution database
intact. Cron keeps running independently. Reverting this cohesive source commit
restores prior cron code; the additive result table is ignored by old code and
requires no downgrade migration. It will remain private local data and consume
storage until an operator separately approves a retention/export/removal policy.
Never delete the whole execution database as an automatic rollback step.

Architecture references (read for this candidate):
[Hermes plugins](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/)
and [cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/).
This is a commissioned fork integration; upstream third-party integrations should
be distributed standalone, not merged into the upstream core tree.
