# Private Journal Plugin

Captures `/log <text>` into a profile-local holding store and processes pending
records with one nightly auxiliary LLM batch. Holding records and receipts are
private retained runtime data under the active profile:

```text
$HERMES_HOME/journal/holding/
```

## Cron wrapper template

Copy `scripts/process_private_journal.py` into the default profile's
`$HERMES_HOME/scripts/` directory, make it executable, and create a Hermes cron
job for midnight with `no_agent=True`. The schedule is:

```cron
0 0 * * * process_private_journal.py
```

The wrapper does not hardcode a vault path. Configure the default profile:

```yaml
plugins:
  entries:
    private-journal:
      vault_path: /path/to/personal-history-log
```

Processing behavior:

- no pending records: success, no LLM call, silent stdout
- pending records: exactly one `private_journal_batch` auxiliary call for the
  unstaged batch
- oversized batch, model failure, invalid model response, write failure, or
  receipt mismatch: nonzero failure; original holding records remain retryable
- crash after staging/output: retry resumes from the private batch manifest
  without a second model call

## Rollback

Disable the `private-journal` plugin and pause or remove the cron entry. Do not
delete `$HERMES_HOME/journal/holding`; immutable captures and receipts are
retained there for retry or audit.
