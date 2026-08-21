# Hermes Release Controller Sandbox Walkthrough

This walkthrough exercises the inactive Phase 1 `hermes-release` controller
without touching the canonical checkout, `~/.hermes`, service units, live
gateway processes, real branches, or real backups.

Run it from a disposable directory. All paths below stay under `$SANDBOX`.

```bash
export SANDBOX="$(mktemp -d)"
export HERMES_RELEASE_SOURCE="$(pwd)"
export PYTHONPATH="$HERMES_RELEASE_SOURCE${PYTHONPATH:+:$PYTHONPATH}"
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
cat > "$SANDBOX/bin/hermes" <<EOF
#!/usr/bin/env bash
set -eu
printf '%s\n' "\$*" >> "$SANDBOX/lifecycle.log"
if [ "\${*: -2:1}" = gateway ] && [ "\${*: -1}" = stop ]; then
  python3 - <<'PY'
import json, pathlib
path = pathlib.Path("$SANDBOX/runtime.json")
data = json.loads(path.read_text())
data.update({"ok": True, "running": False, "stopped": True, "old_pid": data.get("pid", data.get("old_pid"))})
path.write_text(json.dumps(data))
PY
fi
if [ "\${*: -2:1}" = gateway ] && [ "\${*: -1}" = start ]; then
  python3 - <<'PY'
import json, pathlib, subprocess
checkout = pathlib.Path("$SANDBOX/checkout")
path = pathlib.Path("$SANDBOX/runtime.json")
old = json.loads(path.read_text())
branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=checkout, text=True).strip()
sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
path.write_text(json.dumps({
  "ok": True, "running": True, "stopped": False, "pid": int(old.get("pid", 1111)) + 1111,
  "source": str(checkout), "branch": branch, "sha": sha, "service_id": "hermes-gateway-sandbox"
}))
PY
fi
EOF
chmod 700 "$SANDBOX/bin/hermes"
cat > "$SANDBOX/bin/encrypt-archive" <<'EOF'
#!/usr/bin/env python3
import pathlib
import shutil
import sys

src = pathlib.Path(sys.argv[sys.argv.index("--input") + 1])
dst = pathlib.Path(sys.argv[sys.argv.index("--output") + 1])
shutil.copyfile(src, dst)
EOF
chmod 700 "$SANDBOX/bin/encrypt-archive"
cat > "$SANDBOX/bin/verify-archive" <<'EOF'
#!/usr/bin/env python3
import hashlib
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[sys.argv.index("--output") + 1])
expected = sys.argv[sys.argv.index("--sha256") + 1]
actual = hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps({
  "ok": actual == expected,
  "encrypted": True,
  "artifact_sha256": actual,
  "operation_id": sys.argv[sys.argv.index("--operation-id") + 1],
  "candidate_sha": sys.argv[sys.argv.index("--candidate-sha") + 1],
  "rollback_sha": sys.argv[sys.argv.index("--rollback-sha") + 1],
}))
EOF
chmod 700 "$SANDBOX/bin/verify-archive"
```

Create deterministic machine-readable receipts and probes:

```bash
for name in ci review; do
  cat > "$SANDBOX/receipts/$name.json" <<EOF
{"sha":"$CANDIDATE_SHA","status":"ok","issued_at":"2026-08-20T12:00:00Z"}
EOF
done
cat > "$SANDBOX/receipts/authorization.json" <<EOF
{"sha":"$CANDIDATE_SHA","status":"ok","operation":"stage","reference_id":"OOB-SANDBOX","not_before":"2026-08-20T12:00:00Z","expires_at":"2026-08-20T13:00:00Z","single_use":true}
EOF
cat > "$SANDBOX/receipts/compatibility.json" <<EOF
{"sha":"$CANDIDATE_SHA","status":"ok","issued_at":"2026-08-20T12:00:00Z","scope":["repo_local"]}
EOF
cat > "$SANDBOX/receipts/backup.json" <<EOF
{"sha":"$CANDIDATE_SHA","status":"ok","issued_at":"2026-08-20T12:00:00Z","scope":["repo_local"]}
EOF
cat > "$SANDBOX/writers.json" <<EOF
{"ok":true,"active":[]}
EOF
cat > "$SANDBOX/runtime.json" <<EOF
{"ok":true,"running":true,"stopped":false,"pid":1111,"source":"$SANDBOX/checkout","branch":"main","sha":"$(git -C "$SANDBOX/checkout" rev-parse HEAD)","service_id":"hermes-gateway-sandbox"}
EOF
cat > "$SANDBOX/smoke.json" <<EOF
{"ok":true}
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
  "service_id": "hermes-gateway-sandbox",
  "release_scopes": ["repo_local"],
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
  "archive_encryption": {
    "argv": ["$SANDBOX/bin/encrypt-archive", "--input", "{input}", "--output", "{output}"],
    "verify_argv": ["$SANDBOX/bin/verify-archive", "--output", "{output}", "--sha256", "{sha256}", "--operation-id", "{operation_id}", "--candidate-sha", "{candidate_sha}", "--rollback-sha", "{rollback_sha}"],
    "output": "$SANDBOX/encrypted/{operation_id}-repo-local.tar.gz.enc"
  },
  "probes": {
    "runtime": ["python3", "-c", "import pathlib; print(pathlib.Path('$SANDBOX/runtime.json').read_text())"],
    "writers": ["python3", "-c", "import pathlib; print(pathlib.Path('$SANDBOX/writers.json').read_text())"],
    "smoke": ["python3", "-c", "import json,pathlib,subprocess; checkout=pathlib.Path('$SANDBOX/checkout'); base=json.loads(pathlib.Path('$SANDBOX/smoke.json').read_text()); branch=subprocess.check_output(['git','branch','--show-current'],cwd=checkout,text=True).strip(); sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=checkout,text=True).strip(); runtime=json.loads(pathlib.Path('$SANDBOX/runtime.json').read_text()); payload={'ok':base.get('ok') is True,'source':str(checkout),'branch':branch,'sha':sha,'pid':runtime.get('pid'),'service_id':runtime.get('service_id')}; print(json.dumps(payload))"],
    "rollback_safety": ["python3", "-c", "import pathlib; print(pathlib.Path('$SANDBOX/rollback-safe.json').read_text())"]
  },
  "forbidden_live_paths": ["/home/brian/.hermes/config.yaml", "/home/brian/.hermes/hermes-agent"]
}
EOF
```

Expected command flow:

```bash
PYTHONPATH="$HERMES_RELEASE_SOURCE" python -m hermes_release --config "$SANDBOX/release-config.json" preflight "$CANDIDATE_SHA"
PYTHONPATH="$HERMES_RELEASE_SOURCE" python -m hermes_release --config "$SANDBOX/release-config.json" stage "$CANDIDATE_SHA" --authorize OOB-SANDBOX
PYTHONPATH="$HERMES_RELEASE_SOURCE" python -m hermes_release --config "$SANDBOX/release-config.json" status
PYTHONPATH="$HERMES_RELEASE_SOURCE" python -m hermes_release --config "$SANDBOX/release-config.json" rollback
cat > "$SANDBOX/receipts/authorization.json" <<EOF
{"sha":"$CANDIDATE_SHA","status":"ok","operation":"stage","reference_id":"OOB-SANDBOX-2","not_before":"2026-08-20T12:00:00Z","expires_at":"2026-08-20T13:00:00Z","single_use":true}
EOF
PYTHONPATH="$HERMES_RELEASE_SOURCE" python -m hermes_release --config "$SANDBOX/release-config.json" stage "$CANDIDATE_SHA" --authorize OOB-SANDBOX-2
cat > "$SANDBOX/receipts/authorization.json" <<EOF
{"sha":"$CANDIDATE_SHA","status":"ok","operation":"promote","reference_id":"OOB-SANDBOX-3","not_before":"2026-08-20T12:00:00Z","expires_at":"2026-08-20T13:00:00Z","single_use":true}
EOF
PYTHONPATH="$HERMES_RELEASE_SOURCE" python -m hermes_release --config "$SANDBOX/release-config.json" promote "$CANDIDATE_SHA" --authorize OOB-SANDBOX-3
PYTHONPATH="$HERMES_RELEASE_SOURCE" python -m hermes_release --config "$SANDBOX/release-config.json" rollback
```

Expected evidence:

- `preflight` returns JSON with `"ok": true`, `"dry_run": true`, and creates no
  `$SANDBOX/state` directory.
- `stage` returns `"state": "staging-active"`, switches `$SANDBOX/checkout` to
  `staging`, and appends `gateway stop` then `gateway start` to
  `$SANDBOX/lifecycle.log`. It also persists only hashed authorization
  evidence, not the raw `OOB-*` value.
- `status` returns the durable state from
  `$SANDBOX/state/release-state.json`.
- The first `rollback` returns `"state": "rolled-back"` and switches the
  checkout back to `main`.
- `promote` returns `"state": "promoted"`, fast-forwards local and remote
  `main` to `$CANDIDATE_SHA`, and restarts through the fake lifecycle command.
- If `release_scopes` includes `config_secrets`, `database_schema`, or
  `dependencies`, the backup/compatibility receipts must include the matching
  encrypted/private artifact, integrity, restore/list, and rollback
  compatibility proof fields. The minimal `repo_local` scope above accepts the
  compact receipt.
- If sensitive repo-local files such as `.env` exist, the configured
  `archive_encryption.argv`, `archive_encryption.verify_argv`, and
  `archive_encryption.output` encrypt a temporary plaintext tarball with
  `shell=False`, verify JSON `{ok:true, encrypted:true, artifact_sha256:<sha>,
  operation_id:<id>, candidate_sha:<sha>, rollback_sha:<sha>}` against the
  exact output SHA-256 and release operation/SHA bindings, and delete plaintext
  before mutation.
  Use `{operation_id}` in the output path as shown above so repeated release
  operations preserve distinct encrypted artifacts; the controller refuses to
  overwrite an existing resolved output.
- The final `rollback` fails closed with
  `"code": "post_promotion_rollback_refused"` and tells the operator to recover
  with a normal revert or recovery commit rather than rewriting published
  `main`.
- `$SANDBOX/state/release-journal.jsonl` is append-only JSONL evidence. It must
  not contain secret values.
