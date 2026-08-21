# Hermes Release Controller Sandbox Walkthrough

This walkthrough exercises the inactive Phase 1 `hermes-release` controller
without touching the canonical checkout, `~/.hermes`, service units, live
gateway processes, real branches, or real backups.

Run it from a disposable directory. All paths below stay under `$SANDBOX`.

```bash
export SANDBOX="$(mktemp -d)"
mkdir -p "$SANDBOX/bin" "$SANDBOX/receipts"
git init --bare "$SANDBOX/origin.git"
git clone "$SANDBOX/origin.git" "$SANDBOX/seed"
git -C "$SANDBOX/seed" config user.email test@example.invalid
git -C "$SANDBOX/seed" config user.name "Release Test"
printf 'main\n' > "$SANDBOX/seed/app.txt"
git -C "$SANDBOX/seed" add app.txt
git -C "$SANDBOX/seed" commit -m main
git -C "$SANDBOX/seed" branch -M main
git -C "$SANDBOX/seed" push -u origin main
git -C "$SANDBOX/seed" checkout -b staging
printf 'staging\n' > "$SANDBOX/seed/app.txt"
git -C "$SANDBOX/seed" commit -am staging
git -C "$SANDBOX/seed" push -u origin staging
git clone "$SANDBOX/origin.git" "$SANDBOX/checkout"
git -C "$SANDBOX/checkout" checkout main
git -C "$SANDBOX/checkout" checkout -b staging origin/staging
git -C "$SANDBOX/checkout" checkout main
export CANDIDATE_SHA="$(git -C "$SANDBOX/checkout" rev-parse refs/heads/staging)"
cat > "$SANDBOX/bin/hermes" <<'EOF'
#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$SANDBOX/lifecycle.log"
EOF
chmod 700 "$SANDBOX/bin/hermes"
```

Create deterministic machine-readable receipts and probes:

```bash
for name in authorization ci review compatibility backup; do
  cat > "$SANDBOX/receipts/$name.json" <<EOF
{"sha":"$CANDIDATE_SHA","status":"ok","issued_at":"2026-08-20T12:00:00Z"}
EOF
done
cat > "$SANDBOX/writers.json" <<EOF
{"ok":true,"active":[]}
EOF
cat > "$SANDBOX/runtime.json" <<EOF
{"ok":true,"pid":1111,"source":"$SANDBOX/checkout"}
EOF
cat > "$SANDBOX/smoke.json" <<EOF
{"ok":true,"pid":2222,"source":"$SANDBOX/checkout","sha":"$CANDIDATE_SHA"}
EOF
cat > "$SANDBOX/rollback-safe.json" <<EOF
{"ok":true}
EOF
cat > "$SANDBOX/release-config.json" <<EOF
{
  "checkout_path": "$SANDBOX/checkout",
  "state_dir": "$SANDBOX/state",
  "current_time": "2026-08-20T12:10:00Z",
  "remote": "origin",
  "main_branch": "main",
  "staging_branch": "staging",
  "reproducible_untracked_globs": [".venv/**", "node_modules/**", "__pycache__/**"],
  "authorization_receipt": "$SANDBOX/receipts/authorization.json",
  "ci_receipt": "$SANDBOX/receipts/ci.json",
  "review_receipt": "$SANDBOX/receipts/review.json",
  "compatibility_receipt": "$SANDBOX/receipts/compatibility.json",
  "backup_receipt": "$SANDBOX/receipts/backup.json",
  "backup_max_age_seconds": 3600,
  "lifecycle": {
    "stop": ["$SANDBOX/bin/hermes", "gateway", "stop"],
    "start": ["$SANDBOX/bin/hermes", "gateway", "start"],
    "timeout_seconds": 5
  },
  "probes": {
    "runtime": ["python3", "-c", "import pathlib; print(pathlib.Path('$SANDBOX/runtime.json').read_text())"],
    "writers": ["python3", "-c", "import pathlib; print(pathlib.Path('$SANDBOX/writers.json').read_text())"],
    "smoke": ["python3", "-c", "import pathlib; print(pathlib.Path('$SANDBOX/smoke.json').read_text())"],
    "rollback_safety": ["python3", "-c", "import pathlib; print(pathlib.Path('$SANDBOX/rollback-safe.json').read_text())"]
  },
  "forbidden_live_paths": ["/home/brian/.hermes/config.yaml", "/home/brian/.hermes/hermes-agent"]
}
EOF
```

Expected command flow:

```bash
python -m hermes_release --config "$SANDBOX/release-config.json" preflight "$CANDIDATE_SHA"
python -m hermes_release --config "$SANDBOX/release-config.json" stage "$CANDIDATE_SHA" --authorize OOB-SANDBOX
python -m hermes_release --config "$SANDBOX/release-config.json" status
python -m hermes_release --config "$SANDBOX/release-config.json" rollback
python -m hermes_release --config "$SANDBOX/release-config.json" stage "$CANDIDATE_SHA" --authorize OOB-SANDBOX-2
python -m hermes_release --config "$SANDBOX/release-config.json" promote "$CANDIDATE_SHA" --authorize OOB-SANDBOX-3
python -m hermes_release --config "$SANDBOX/release-config.json" rollback
```

Expected evidence:

- `preflight` returns JSON with `"ok": true`, `"dry_run": true`, and creates no
  `$SANDBOX/state` directory.
- `stage` returns `"state": "staging-active"`, switches `$SANDBOX/checkout` to
  `staging`, and appends `gateway stop` then `gateway start` to
  `$SANDBOX/lifecycle.log`.
- `status` returns the durable state from
  `$SANDBOX/state/release-state.json`.
- The first `rollback` returns `"state": "rolled-back"` and switches the
  checkout back to `main`.
- `promote` returns `"state": "promoted"`, fast-forwards local and remote
  `main` to `$CANDIDATE_SHA`, and restarts through the fake lifecycle command.
- The final `rollback` fails closed with
  `"code": "post_promotion_rollback_refused"` and tells the operator to recover
  with a normal revert or recovery commit rather than rewriting published
  `main`.
- `$SANDBOX/state/release-journal.jsonl` is append-only JSONL evidence. It must
  not contain secret values.
