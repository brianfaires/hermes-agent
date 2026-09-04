# disk-cleanup

Auto-tracks and cleans up ephemeral files created during Hermes Agent
sessions — test scripts, temp outputs, cron logs, stale chrome profiles.
Scoped strictly to `$HERMES_HOME` and `/tmp/hermes-*`.

Originally contributed by [@LVT382009](https://github.com/LVT382009) as a
skill in PR #12212.  Ported to the plugin system so the behaviour runs
automatically via `post_tool_call` and `on_session_end` hooks — the agent
never needs to remember to call a tool.

## How it works

| Hook | Behaviour |
|---|---|
| `post_tool_call` | When `write_file` / `terminal` / `patch` creates a disposable file matching `test_*`, `tmp_*`, or `*.test.*` inside `HERMES_HOME`, track it silently as `test` / `temp` / `cron-output`. Durable script trees are excluded. |
| `on_session_end` | If any test files were auto-tracked during this turn, run `quick` cleanup (no prompts). |

Deletion rules (same as the original PR):

| Category | Threshold | Confirmation |
|---|---|---|
| `test` | every session end | Never |
| `temp` | >7 days since tracked | Never |
| `cron-output` | >14 days since tracked | Never |
| empty dirs under HERMES_HOME | always | Never |
| `research` | >30 days, beyond 10 newest | Always (deep only) |
| `chrome-profile` | >14 days since tracked | Always (deep only) |
| files >500 MB | never auto | Always (deep only) |

## Slash command

```
/disk-cleanup status                     # breakdown + top-10 largest
/disk-cleanup dry-run                    # preview without deleting
/disk-cleanup quick                      # run safe cleanup now
/disk-cleanup deep                       # quick + list items needing prompt
/disk-cleanup track <path> <category>    # manual tracking
/disk-cleanup forget <path>              # stop tracking
```

## Wildcard policies

A tracked path may end in `/*` to create a persistent cleanup policy over a
safe directory's descendants. The wildcard must be exactly an absolute parent
path followed by `/*`, for example:

```
$HERMES_HOME/audio_cache/*
$HERMES_HOME/cron/output/*
```

For wildcard policies, cleanup evaluates each descendant regular file by that
file's own mtime and the stored category: `test` files are eligible
immediately, `temp` files after 7 days, and `cron-output` files after 14 days.
`dry-run` lists the eligible descendant files, not the wildcard policy record.
`quick` deletes eligible files, leaves young files in place, removes descendant
directories only after they become empty, keeps the wildcard parent directory,
and retains the wildcard policy for future runs.

Malformed wildcard strings, unsupported wildcard shapes, unsafe parents,
symlink escapes, `$HERMES_HOME/*`, protected top-level trees such as
`logs/*`, `memories/*`, `sessions/*`, `skills/*`, `plugins/*`, `profiles/*`,
cron control-plane trees other than exact `cron/output/*`, and durable script
trees are ignored or rejected.

## Safety

- `is_safe_path()` rejects anything outside `HERMES_HOME` or `/tmp/hermes-*`
- Windows mounts (`/mnt/c` etc.) are rejected
- The state directory `$HERMES_HOME/disk-cleanup/` is itself excluded
- `$HERMES_HOME/logs/`, `memories/`, `sessions/`, `skills/`, `plugins/`,
  and config files are never tracked
- Wildcard policies rooted at `$HERMES_HOME` or durable top-level trees are
  rejected by `track` and ignored/dropped when found in stale manual state
- Durable script trees at `$HERMES_HOME/scripts` and
  `$HERMES_HOME/profiles/<name>/scripts` are never auto-tracked, shown as
  dry-run deletion candidates, or deleted from stale/manual tracked state,
  regardless of their stored category
- Wildcard cleanup never follows symlinked files or directories and never
  deletes the wildcard parent directory itself
- Backup/restore is scoped to `tracked.json` — the plugin never touches
  agent logs
- Atomic writes: `.tmp` → backup → rename
