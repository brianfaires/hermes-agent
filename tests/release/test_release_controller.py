from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def run(cmd: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise AssertionError(f"{cmd} failed\nstdout={result.stdout}\nstderr={result.stderr}")
    return result


def git(cwd: Path, *args: str) -> str:
    return run(["git", *args], cwd).stdout.strip()


def commit_file(repo: Path, name: str, content: str, message: str) -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def init_release_repo(tmp_path: Path) -> dict[str, Path | str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    remote = tmp_path / "remote.git"
    run(["git", "init", "--bare", str(remote)], tmp_path)

    seed = tmp_path / "seed"
    run(["git", "clone", str(remote), str(seed)], tmp_path)
    git(seed, "config", "user.email", "test@example.invalid")
    git(seed, "config", "user.name", "Release Test")
    main_sha = commit_file(seed, "app.txt", "main\n", "main")
    git(seed, "branch", "-M", "main")
    git(seed, "push", "-u", "origin", "main")
    git(seed, "checkout", "-b", "staging")
    staging_sha = commit_file(seed, "app.txt", "staging\n", "staging")
    git(seed, "push", "-u", "origin", "staging")

    checkout = tmp_path / "checkout"
    run(["git", "clone", str(remote), str(checkout)], tmp_path)
    git(checkout, "checkout", "main")
    git(checkout, "checkout", "-b", "staging", "origin/staging")
    git(checkout, "checkout", "main")
    git(checkout, "config", "user.email", "test@example.invalid")
    git(checkout, "config", "user.name", "Release Test")

    return {
        "remote": remote,
        "seed": seed,
        "checkout": checkout,
        "main_sha": main_sha,
        "staging_sha": staging_sha,
    }


def write_fake_hermes(path: Path, log_path: Path, runtime_path: Path, checkout: Path, service_id: str, *, fail_start: bool = False) -> None:
    start_guard = (
        f"  if [ \"$(git -C {str(checkout)!r} branch --show-current)\" = staging ]; then exit 3; fi\n"
        if fail_start
        else ""
    )
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        f"printf '%s\\n' \"$*\" >> {str(log_path)!r}\n"
        "if [ \"$#\" -ge 2 ] && [ \"${@: -2:1}\" = gateway ] && [ \"${@: -1}\" = stop ]; then\n"
        f"  python3 - <<'PY'\n"
        "import json, pathlib\n"
        f"path = pathlib.Path({str(runtime_path)!r})\n"
        "data = json.loads(path.read_text())\n"
        "data.update({'ok': True, 'running': False, 'stopped': True, 'old_pid': data.get('pid')})\n"
        "path.write_text(json.dumps(data))\n"
        "PY\n"
        "fi\n"
        "if [ \"$#\" -ge 2 ] && [ \"${@: -2:1}\" = gateway ] && [ \"${@: -1}\" = start ]; then\n"
        f"{start_guard}"
        f"  python3 - <<'PY'\n"
        "import json, pathlib, subprocess\n"
        f"path = pathlib.Path({str(runtime_path)!r})\n"
        f"checkout = pathlib.Path({str(checkout)!r})\n"
        "old = json.loads(path.read_text())\n"
        "branch = subprocess.check_output(['git', 'branch', '--show-current'], cwd=checkout, text=True).strip()\n"
        "sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip()\n"
        "pid = int(old.get('old_pid') or old.get('pid') or 1111) + 1111\n"
        f"data = {{'ok': True, 'running': True, 'stopped': False, 'pid': pid, 'source': str(checkout), 'branch': branch, 'sha': sha, 'service_id': {service_id!r}}}\n"
        "path.write_text(json.dumps(data))\n"
        "PY\n"
        "fi\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


def write_receipt(
    path: Path,
    *,
    sha: str,
    now: str = "2026-08-20T12:00:00Z",
    ok: bool = True,
    kind: str = "ci",
    operation: str | None = None,
    reference_id: str = "OOB-123",
    not_before: str = "2026-08-20T12:00:00Z",
    expires_at: str = "2026-08-20T13:00:00Z",
    scopes: list[str] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {"sha": sha, "status": "ok" if ok else "failed", "issued_at": now}
    if kind == "authorization":
        data.update(
            {
                "operation": operation or "stage",
                "reference_id": reference_id,
                "not_before": not_before,
                "expires_at": expires_at,
                "single_use": True,
            }
        )
    elif kind == "backup":
        declared = scopes or ["repo_local"]
        data["scope"] = declared
        if "config_secrets" in declared:
            data["config_secrets"] = {
                "private": True,
                "encrypted": True,
                "artifact_sha256": "a" * 64,
                "restore_verified": True,
            }
        if "database_schema" in declared:
            data["database_schema"] = {
                "method": "sqlite-online",
                "integrity_ok": True,
                "encrypted": True,
                "artifact_sha256": "b" * 64,
                "restore_list_verified": True,
            }
    elif kind == "compatibility":
        declared = scopes or ["repo_local"]
        data["scope"] = declared
        if "dependencies" in declared or "database_schema" in declared:
            data["rollback_compatibility"] = {"rollback_sha": sha, "backward": True, "rollback": True}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_config(
    tmp_path: Path,
    checkout: Path,
    sha: str,
    *,
    start_fails: bool = False,
    smoke_ok: bool = True,
    backup_sha: str | None = None,
    authorization: bool = True,
    writer_active: bool = False,
    auth_operation: str = "stage",
    auth_reference: str = "OOB-123",
    release_scopes: list[str] | None = None,
) -> Path:
    state_dir = tmp_path / "state"
    receipts = tmp_path / "receipts"
    lifecycle_log = tmp_path / "lifecycle.log"
    fake_hermes = tmp_path / "bin" / "hermes"
    fake_hermes.parent.mkdir(exist_ok=True)
    service_id = "hermes-gateway-test"
    smoke = tmp_path / "smoke.json"
    smoke.write_text(json.dumps({"ok": smoke_ok}), encoding="utf-8")
    rollback = tmp_path / "rollback-safe.json"
    rollback.write_text(json.dumps({"ok": True}), encoding="utf-8")
    writers = tmp_path / "writers.json"
    writers.write_text(json.dumps({"ok": not writer_active, "active": ["writer"] if writer_active else []}), encoding="utf-8")
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "ok": True,
                "running": True,
                "stopped": False,
                "pid": 1111,
                "source": str(checkout),
                "branch": "main",
                "sha": git(checkout, "rev-parse", "HEAD"),
                "service_id": service_id,
            }
        ),
        encoding="utf-8",
    )
    write_fake_hermes(fake_hermes, lifecycle_log, runtime, checkout, service_id, fail_start=start_fails)
    auth_path = receipts / "authorization.json"
    if authorization:
        write_receipt(auth_path, sha=sha, kind="authorization", operation=auth_operation, reference_id=auth_reference)
    scopes = release_scopes or ["repo_local"]
    config = {
        "checkout_path": str(checkout),
        "state_dir": str(state_dir),
        "current_time": "2026-08-20T12:10:00Z",
        "remote": "origin",
        "main_branch": "main",
        "staging_branch": "staging",
        "reproducible_untracked_globs": [".venv/**", "node_modules/**", "__pycache__/**"],
        "release_scopes": scopes,
        "authorization_receipt": str(auth_path),
        "ci_receipt": str(write_receipt(receipts / "ci.json", sha=sha)),
        "review_receipt": str(write_receipt(receipts / "review.json", sha=sha)),
        "compatibility_receipt": str(write_receipt(receipts / "compat.json", sha=sha, kind="compatibility", scopes=scopes)),
        "backup_receipt": str(write_receipt(receipts / "backup.json", sha=backup_sha or sha, kind="backup", scopes=scopes)),
        "backup_max_age_seconds": 3600,
        "lifecycle": {
            "stop": [str(fake_hermes), "gateway", "stop"],
            "start": [str(fake_hermes), "gateway", "start"],
            "timeout_seconds": 5,
        },
        "probes": {
            "runtime": [sys.executable, "-c", f"import pathlib; print(pathlib.Path({str(runtime)!r}).read_text())"],
            "writers": [sys.executable, "-c", f"import pathlib; print(pathlib.Path({str(writers)!r}).read_text())"],
            "smoke": [
                sys.executable,
                "-c",
                (
                    "import json,pathlib,subprocess;"
                    f"checkout=pathlib.Path({str(checkout)!r});"
                    f"base=json.loads(pathlib.Path({str(smoke)!r}).read_text());"
                    "branch=subprocess.check_output(['git','branch','--show-current'],cwd=checkout,text=True).strip();"
                    "sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=checkout,text=True).strip();"
                    "runtime=json.loads(pathlib.Path("
                    f"{str(runtime)!r}"
                    ").read_text());"
                    "identity={'source':str(checkout),'branch':branch,'sha':sha,'pid':runtime.get('pid'),'service_id':runtime.get('service_id')};"
                    "bad=base.pop('bad_on_staging', None) if branch == 'staging' else base.pop('bad_on_main', None) if branch == 'main' else None;"
                    "identity.update(base);"
                    "identity.update(bad or {});"
                    "print(json.dumps(identity))"
                ),
            ],
            "rollback_safety": [sys.executable, "-c", f"import pathlib; print(pathlib.Path({str(rollback)!r}).read_text())"],
        },
        "forbidden_live_paths": ["/home/brian/.hermes/config.yaml", "/home/brian/.hermes/hermes-agent"],
    }
    path = tmp_path / "release-config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def rewrite_auth(config: Path, sha: str, operation: str, reference_id: str, **kwargs: object) -> None:
    data = json.loads(config.read_text(encoding="utf-8"))
    write_receipt(Path(data["authorization_receipt"]), sha=sha, kind="authorization", operation=operation, reference_id=reference_id, **kwargs)


def cli(config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hermes_release", "--config", str(config), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def cli_with_global_dry_run(config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hermes_release", "--config", str(config), "--dry-run", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def assert_success(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def assert_failure(result: subprocess.CompletedProcess[str], code: str) -> dict:
    assert result.returncode != 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == code
    assert payload["recovery_action"]
    return payload


def test_preflight_is_non_mutating_and_redacts_secret_values(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    (checkout / ".env").write_text("TOKEN=super-secret-token\n", encoding="utf-8")
    config = write_config(tmp_path, checkout, sha)

    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    result = assert_success(cli(config, "preflight", sha))
    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))

    assert before == after
    assert result["command"] == "preflight"
    assert result["dry_run"] is True
    assert result["candidate_sha"] == sha
    rendered = json.dumps(result)
    assert "super-secret-token" not in rendered
    assert "<redacted" in rendered


def test_stage_dry_run_is_non_mutating(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)

    result = assert_success(cli_with_global_dry_run(config, "stage", sha, "--authorize", "OOB-DRY"))

    assert result["command"] == "preflight"
    assert result["dry_run"] is True
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "lifecycle.log").exists()
    assert git(checkout, "branch", "--show-current") == "main"


def test_stage_status_promote_and_stage_rollback_happy_path(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)

    staged = assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))
    assert staged["state"] == "staging-active"
    assert git(checkout, "branch", "--show-current") == "staging"
    assert git(checkout, "rev-parse", "HEAD") == sha
    assert (tmp_path / "lifecycle.log").read_text(encoding="utf-8").splitlines() == [
        "gateway stop",
        "gateway start",
    ]

    status = assert_success(cli(config, "status"))
    assert status["state"] == "staging-active"
    assert status["candidate_sha"] == sha

    rolled = assert_success(cli(config, "rollback"))
    assert rolled["state"] == "rolled-back"
    assert git(checkout, "branch", "--show-current") == "main"
    assert git(checkout, "rev-parse", "HEAD") == repo["main_sha"]

    rewrite_auth(config, sha, "stage", "OOB-456")
    assert_success(cli(config, "stage", sha, "--authorize", "OOB-456"))
    rewrite_auth(config, sha, "promote", "OOB-789")
    promoted = assert_success(cli(config, "promote", sha, "--authorize", "OOB-789"))
    assert promoted["state"] == "promoted"
    assert git(checkout, "branch", "--show-current") == "main"
    assert git(checkout, "rev-parse", "HEAD") == sha
    assert git(checkout, "rev-parse", "refs/remotes/origin/main") == sha

    refused = assert_failure(cli(config, "rollback"), "post_promotion_rollback_refused")
    assert "revert" in refused["recovery_action"].lower()


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda checkout, sha, tmp_path: (checkout / "app.txt").write_text("dirty\n", encoding="utf-8"), "dirty_checkout"),
        (lambda checkout, sha, tmp_path: (tmp_path / "receipts" / "backup.json").unlink(), "missing_receipt"),
        (lambda checkout, sha, tmp_path: (write_receipt(tmp_path / "receipts" / "backup.json", sha=sha, now="2026-08-19T10:00:00Z", kind="backup"), None)[1], "stale_receipt"),
        (lambda checkout, sha, tmp_path: write_config(tmp_path, checkout, sha, backup_sha="0" * 40), "receipt_sha_mismatch"),
        (
            lambda checkout, sha, tmp_path: (
                git(checkout, "checkout", "staging"),
                commit_file(checkout, "drift.txt", "drift\n", "drift"),
                git(checkout, "push", "origin", "staging"),
                git(checkout, "checkout", "main"),
                None,
            )[-1],
            "sha_drift",
        ),
        (lambda checkout, sha, tmp_path: write_config(tmp_path, checkout, sha, writer_active=True), "active_writers"),
    ],
)
def test_preflight_fails_closed_for_required_gates(tmp_path: Path, mutate, code: str) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    maybe_config = mutate(checkout, sha, tmp_path)
    if isinstance(maybe_config, Path):
        config = maybe_config

    assert_failure(cli(config, "preflight", sha), code)


def test_stage_refuses_when_staging_branch_checked_out_elsewhere(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    other = tmp_path / "staging-worktree"
    git(checkout, "worktree", "add", str(other), "staging")
    config = write_config(tmp_path, checkout, sha)

    assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "branch_checked_out_elsewhere")


def test_lock_contention_fails_without_mutation(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    lock = tmp_path / "state" / "release.lock"
    lock.parent.mkdir()
    lock.write_text("held", encoding="utf-8")

    stale_lock = assert_success(cli(config, "preflight", sha))
    assert stale_lock["ok"] is True
    rewrite_auth(config, sha, "stage", "OOB-123")
    assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))


def test_active_advisory_lock_fails_without_mutation(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    lock = tmp_path / "state" / "release.lock"
    lock.parent.mkdir()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,pathlib,time;"
                f"p=pathlib.Path({str(lock)!r});"
                "f=p.open('w');"
                "fcntl.flock(f.fileno(), fcntl.LOCK_EX);"
                "time.sleep(10)"
            ),
        ]
    )
    try:
        time.sleep(0.5)
        assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "lock_contended")
        assert git(checkout, "branch", "--show-current") == "main"
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_process_death_releases_advisory_lock(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    lock = tmp_path / "state" / "release.lock"
    lock.parent.mkdir()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,pathlib;"
                f"p=pathlib.Path({str(lock)!r});"
                "f=p.open('w');"
                "fcntl.flock(f.fileno(), fcntl.LOCK_EX)"
            ),
        ]
    )
    holder.wait(timeout=5)
    rewrite_auth(config, sha, "stage", "OOB-123")
    assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))


def test_authoritative_remote_refs_ignore_stale_tracking_refs(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    before_tracking = git(checkout, "rev-parse", "refs/remotes/origin/main")
    main2 = commit_file(repo["seed"], "remote-main.txt", "advanced\n", "advance main")
    git(repo["seed"], "checkout", "main")
    git(repo["seed"], "cherry-pick", main2)
    git(repo["seed"], "push", "origin", "main")

    failed = assert_failure(cli(config, "preflight", sha), "main_ref_drift")

    assert failed["details"]["remote_main"] != before_tracking
    assert git(checkout, "rev-parse", "refs/remotes/origin/main") == before_tracking


def test_state_dir_and_archive_must_not_be_inside_checkout(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    data = json.loads(config.read_text(encoding="utf-8"))
    data["state_dir"] = str(checkout / ".release-state")
    config.write_text(json.dumps(data), encoding="utf-8")

    assert_failure(cli(config, "preflight", sha), "unsafe_state_location")


@pytest.mark.parametrize(
    ("mutate_auth", "code"),
    [
        (lambda config, sha: Path(json.loads(config.read_text())["authorization_receipt"]).unlink(), "missing_authorization"),
        (lambda config, sha: rewrite_auth(config, sha, "promote", "OOB-123"), "authorization_operation_mismatch"),
        (lambda config, sha: rewrite_auth(config, sha, "stage", "OTHER"), "authorization_id_mismatch"),
        (lambda config, sha: rewrite_auth(config, sha, "stage", "OOB-123", expires_at="2026-08-20T12:05:00Z"), "authorization_expired"),
        (lambda config, sha: rewrite_auth(config, sha, "stage", "OOB-123", not_before="2026-08-20T12:30:00Z"), "authorization_not_yet_valid"),
    ],
)
def test_stage_authorization_receipt_is_mandatory_exact_and_in_window(tmp_path: Path, mutate_auth, code: str) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    mutate_auth(config, sha)

    assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), code)


def test_authorization_receipt_is_single_use_and_redacted(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)

    assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))
    rewrite_auth(config, sha, "promote", "OOB-123")
    assert_failure(cli(config, "promote", sha, "--authorize", "OOB-123"), "authorization_reused")
    journal = (tmp_path / "state" / "release-journal.jsonl").read_text(encoding="utf-8")
    state = (tmp_path / "state" / "release-state.json").read_text(encoding="utf-8")
    assert "OOB-123" not in journal
    assert "OOB-123" not in state


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("pid", 9999, "runtime_identity_mismatch"),
        ("source", "/tmp/wrong-source", "runtime_identity_mismatch"),
        ("branch", "main", "runtime_identity_mismatch"),
        ("sha", "0" * 40, "runtime_identity_mismatch"),
        ("service_id", "wrong-service", "runtime_identity_mismatch"),
    ],
)
def test_stage_fails_closed_for_wrong_started_runtime_identity(tmp_path: Path, field: str, value: object, code: str) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    data = json.loads(config.read_text(encoding="utf-8"))
    smoke = tmp_path / "smoke.json"
    smoke.write_text(json.dumps({"ok": True, "bad_on_staging": {field: value}}), encoding="utf-8")

    assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), code)


@pytest.mark.parametrize(
    ("scope", "receipt_name", "replacement", "code"),
    [
        ("config_secrets", "backup_receipt", {"sha": "{sha}", "status": "ok", "issued_at": "2026-08-20T12:00:00Z", "scope": ["config_secrets"]}, "backup_schema_invalid"),
        ("database_schema", "backup_receipt", {"sha": "{sha}", "status": "ok", "issued_at": "2026-08-20T12:00:00Z", "scope": ["database_schema"]}, "backup_schema_invalid"),
        ("dependencies", "compatibility_receipt", {"sha": "{sha}", "status": "ok", "issued_at": "2026-08-20T12:00:00Z", "scope": ["dependencies"]}, "compatibility_schema_invalid"),
    ],
)
def test_scoped_backup_and_compatibility_receipts_fail_closed_when_schema_is_generic(
    tmp_path: Path, scope: str, receipt_name: str, replacement: dict[str, object], code: str
) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha, release_scopes=[scope])
    data = json.loads(config.read_text(encoding="utf-8"))
    rendered = {key: (sha if value == "{sha}" else value) for key, value in replacement.items()}
    Path(data[receipt_name]).write_text(json.dumps(rendered), encoding="utf-8")

    assert_failure(cli(config, "preflight", sha), code)


def test_minimal_code_only_scope_accepts_generic_backup_and_compatibility_receipts(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha, release_scopes=["repo_local"])
    data = json.loads(config.read_text(encoding="utf-8"))
    write_receipt(Path(data["backup_receipt"]), sha=sha)
    write_receipt(Path(data["compatibility_receipt"]), sha=sha)

    assert_success(cli(config, "preflight", sha))


def test_sensitive_repo_local_archive_requires_encryption(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    (checkout / ".env").write_text("TOKEN=super-secret-token\n", encoding="utf-8")
    config = write_config(tmp_path, checkout, sha)

    failed = assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "encrypted_archive_required")

    rendered = json.dumps(failed)
    assert "super-secret-token" not in rendered
    assert ".env" not in rendered


def test_sensitive_repo_local_archive_uses_configured_encryptor(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    (checkout / ".env").write_text("TOKEN=super-secret-token\n", encoding="utf-8")
    config = write_config(tmp_path, checkout, sha)
    encryptor = tmp_path / "bin" / "encrypt-archive"
    encrypted = tmp_path / "encrypted.tar.gz.enc"
    encryptor.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, shutil, sys\n"
        "src = pathlib.Path(sys.argv[sys.argv.index('--input') + 1])\n"
        "dst = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "shutil.copyfile(src, dst)\n",
        encoding="utf-8",
    )
    encryptor.chmod(stat.S_IRWXU)
    data = json.loads(config.read_text(encoding="utf-8"))
    data["archive_encryption"] = {"argv": [str(encryptor), "--input", "{input}", "--output", "{output}"], "output": str(encrypted)}
    config.write_text(json.dumps(data), encoding="utf-8")

    result = assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))

    archive = result["repo_local_archive"]
    assert archive["encrypted"] is True
    assert archive["sha256"]
    assert encrypted.exists()
    assert not Path(archive["plaintext_path"]).exists()


def test_promotion_startup_failure_after_main_advances_requires_recovery(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))
    smoke = tmp_path / "smoke.json"
    smoke.write_text(json.dumps({"ok": True, "bad_on_main": {"sha": "0" * 40}}), encoding="utf-8")
    rewrite_auth(config, sha, "promote", "OOB-456")

    failed = assert_failure(cli(config, "promote", sha, "--authorize", "OOB-456"), "promotion_recovery_required")

    assert failed["state"] == "promotion-recovery-required"
    assert git(checkout, "branch", "--show-current") == "main"
    assert git(checkout, "rev-parse", "HEAD") == sha
    state = json.loads((tmp_path / "state" / "release-state.json").read_text(encoding="utf-8"))
    assert state["state"] == "promotion-recovery-required"


def test_stage_lifecycle_timeout_failure_and_switch_failure_preserve_evidence(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config_data_path = write_config(tmp_path, checkout, sha)
    data = json.loads(config_data_path.read_text(encoding="utf-8"))
    slow_hermes = tmp_path / "slow" / "hermes"
    slow_hermes.parent.mkdir()
    slow_hermes.write_text("#!/usr/bin/env bash\nsleep 5\n", encoding="utf-8")
    slow_hermes.chmod(0o700)
    data["lifecycle"]["stop"] = [str(slow_hermes), "gateway", "stop"]
    data["lifecycle"]["timeout_seconds"] = 1
    config_data_path.write_text(json.dumps(data), encoding="utf-8")

    assert_failure(cli(config_data_path, "stage", sha, "--authorize", "OOB-123"), "lifecycle_failed")
    journal = tmp_path / "state" / "release-journal.jsonl"
    assert journal.exists()
    assert "stop_gateway_failed" in journal.read_text(encoding="utf-8")

    repo2 = init_release_repo(tmp_path / "switch")
    checkout2 = repo2["checkout"]
    git(checkout2, "checkout", "staging")
    sha2 = commit_file(checkout2, "conflict.txt", "tracked on staging\n", "staging conflict")
    git(checkout2, "push", "origin", "staging")
    git(checkout2, "checkout", "main")
    (checkout2 / "conflict.txt").write_text("untracked local file\n", encoding="utf-8")
    config2 = write_config(tmp_path / "switch", checkout2, sha2)
    assert_failure(cli(config2, "stage", sha2, "--authorize", "OOB-123"), "switch_failed")


def test_startup_failure_rolls_back_when_safe_and_stays_stopped_when_uncertain(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha, start_fails=True)

    failed = assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "startup_failed_rolled_back")
    assert failed["state"] == "rolled-back"
    assert git(checkout, "branch", "--show-current") == "main"

    uncertain = tmp_path / "uncertain"
    repo2 = init_release_repo(uncertain)
    checkout2 = repo2["checkout"]
    sha2 = str(repo2["staging_sha"])
    config2 = write_config(uncertain, checkout2, sha2, start_fails=True)
    rollback_probe = uncertain / "rollback-safe.json"
    rollback_probe.write_text(json.dumps({"ok": False, "reason": "fixture uncertainty"}), encoding="utf-8")
    assert_failure(cli(config2, "stage", sha2, "--authorize", "OOB-123"), "rollback_uncertain")


def test_journal_crash_recovery_and_idempotent_stage_retry(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    git(checkout, "switch", "staging")
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "ok": True,
                "running": True,
                "stopped": False,
                "pid": 2222,
                "source": str(checkout),
                "branch": "staging",
                "sha": sha,
                "service_id": "hermes-gateway-test",
            }
        ),
        encoding="utf-8",
    )

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "release-state.json").write_text(
        json.dumps(
            {
                "operation_id": "op-crashed",
                "state": "staging-active",
                "candidate_sha": sha,
                "rollback_sha": repo["main_sha"],
                "promoted": False,
            }
        ),
        encoding="utf-8",
    )

    recovered = assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))
    assert recovered["idempotent"] is True
    assert recovered["operation_id"] == "op-crashed"


def test_idempotent_stage_retry_fails_closed_when_state_and_reality_disagree(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "release-state.json").write_text(
        json.dumps(
            {
                "operation_id": "op-stale",
                "state": "staging-active",
                "candidate_sha": sha,
                "rollback_sha": repo["main_sha"],
                "promoted": False,
            }
        ),
        encoding="utf-8",
    )

    assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "stale_state_mismatch")


def test_no_live_system_guard_rejects_live_checkout(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, Path("/home/brian/.hermes/hermes-agent"), sha)

    assert_failure(cli(config, "preflight", sha), "live_system_guard")
