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
        "data.update({'ok': True, 'running': False, 'stopped': True, 'old_pid': data.get('pid', data.get('old_pid'))})\n"
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
    single_use: bool | None = True,
    rollback_sha: str | None = None,
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
            }
        )
        if single_use is not None:
            data["single_use"] = single_use
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
            data["rollback_compatibility"] = {"rollback_sha": rollback_sha or sha, "backward": True, "rollback": True}
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
    service_id: str = "hermes-gateway-test",
    runtime_service_id: str | None = None,
    writers_payload: dict[str, object] | None = None,
) -> Path:
    state_dir = tmp_path / "state"
    receipts = tmp_path / "receipts"
    lifecycle_log = tmp_path / "lifecycle.log"
    fake_hermes = tmp_path / "bin" / "hermes"
    fake_hermes.parent.mkdir(exist_ok=True)
    smoke = tmp_path / "smoke.json"
    smoke.write_text(json.dumps({"ok": smoke_ok}), encoding="utf-8")
    rollback = tmp_path / "rollback-safe.json"
    rollback.write_text(json.dumps({"ok": True}), encoding="utf-8")
    writers = tmp_path / "writers.json"
    writers.write_text(
        json.dumps(writers_payload if writers_payload is not None else {"ok": not writer_active, "active": ["writer"] if writer_active else []}),
        encoding="utf-8",
    )
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
                "service_id": runtime_service_id or service_id,
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
        "service_id": service_id,
        "authorization_receipt": str(auth_path),
        "ci_receipt": str(write_receipt(receipts / "ci.json", sha=sha)),
        "review_receipt": str(write_receipt(receipts / "review.json", sha=sha)),
        "compatibility_receipt": str(write_receipt(receipts / "compat.json", sha=sha, kind="compatibility", scopes=scopes, rollback_sha=git(checkout, "rev-parse", "HEAD"))),
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


def read_config(config: Path) -> dict[str, object]:
    return json.loads(config.read_text(encoding="utf-8"))


def write_config_data(config: Path, data: dict[str, object]) -> None:
    config.write_text(json.dumps(data), encoding="utf-8")


def replace_stop_with_stopped_runtime_mutation(config: Path, tmp_path: Path, mutation: dict[str, object]) -> None:
    data = read_config(config)
    stop_hermes = tmp_path / "mutating-stop" / "hermes"
    stop_hermes.parent.mkdir(exist_ok=True)
    stop_hermes.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        f"printf '%s\\n' \"$*\" >> {str(tmp_path / 'lifecycle.log')!r}\n"
        "if [ \"${*: -2:1}\" = gateway ] && [ \"${*: -1}\" = stop ]; then\n"
        "python3 - <<'PY'\n"
        "import json, pathlib\n"
        f"path = pathlib.Path({str(tmp_path / 'runtime.json')!r})\n"
        f"mutation = {mutation!r}\n"
        "data = json.loads(path.read_text())\n"
        "data.update({'ok': True, 'running': False, 'stopped': True, 'old_pid': data.get('pid', data.get('old_pid'))})\n"
        "data.update(mutation)\n"
        "path.write_text(json.dumps(data))\n"
        "PY\n"
        "fi\n",
        encoding="utf-8",
    )
    stop_hermes.chmod(0o700)
    data["lifecycle"]["stop"] = [str(stop_hermes), "gateway", "stop"]  # type: ignore[index]
    write_config_data(config, data)


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


def test_real_successful_stage_retry_is_idempotent_with_consumed_authorization(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)

    staged = assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))
    retried = assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))

    assert retried["idempotent"] is True
    assert retried["operation_id"] == staged["operation_id"]
    consumed = json.loads((tmp_path / "state" / "authorization-consumed.json").read_text(encoding="utf-8"))
    assert len(consumed["consumed"]) == 1


def test_promote_dry_run_validates_active_staging_without_writes(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    staged = assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))
    before_state = (tmp_path / "state" / "release-state.json").read_text(encoding="utf-8")
    before_journal = (tmp_path / "state" / "release-journal.jsonl").read_text(encoding="utf-8")
    before_consumed = (tmp_path / "state" / "authorization-consumed.json").read_text(encoding="utf-8")
    rewrite_auth(config, sha, "promote", "OOB-DRY")

    result = assert_success(cli_with_global_dry_run(config, "promote", sha, "--authorize", "OOB-DRY"))

    assert result["command"] == "promote"
    assert result["dry_run"] is True
    assert result["candidate_sha"] == sha
    assert result["operation_id"] == staged["operation_id"]
    assert (tmp_path / "state" / "release-state.json").read_text(encoding="utf-8") == before_state
    assert (tmp_path / "state" / "release-journal.jsonl").read_text(encoding="utf-8") == before_journal
    assert (tmp_path / "state" / "authorization-consumed.json").read_text(encoding="utf-8") == before_consumed
    assert git(checkout, "branch", "--show-current") == "staging"


def test_promote_dry_run_rejects_staging_drift_without_writes(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))
    before_state = (tmp_path / "state" / "release-state.json").read_text(encoding="utf-8")
    git(checkout, "switch", "main")
    rewrite_auth(config, sha, "promote", "OOB-DRY")

    assert_failure(cli_with_global_dry_run(config, "promote", sha, "--authorize", "OOB-DRY"), "stale_state_mismatch")

    assert (tmp_path / "state" / "release-state.json").read_text(encoding="utf-8") == before_state
    consumed = json.loads((tmp_path / "state" / "authorization-consumed.json").read_text(encoding="utf-8"))
    assert len(consumed["consumed"]) == 1


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


@pytest.mark.parametrize("command", ["preflight", "stage", "status"])
def test_static_config_guard_rejects_unsafe_locations_before_any_state_write(tmp_path: Path, command: str) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    data = read_config(config)
    data["state_dir"] = str(checkout / ".release-state")
    write_config_data(config, data)

    before = sorted(p.relative_to(checkout) for p in checkout.rglob("*"))
    args = [command] if command == "status" else [command, sha, "--authorize", "OOB-123"] if command == "stage" else [command, sha]

    assert_failure(cli(config, *args), "unsafe_state_location")

    assert sorted(p.relative_to(checkout) for p in checkout.rglob("*")) == before
    assert not (checkout / ".release-state").exists()
    assert not (tmp_path / "state").exists()


def test_archive_encryption_output_inside_checkout_is_rejected_before_state_write(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    (checkout / ".env").write_text("TOKEN=super-secret-token\n", encoding="utf-8")
    config = write_config(tmp_path, checkout, sha)
    encryptor = tmp_path / "bin" / "encrypt-archive"
    encryptor.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    encryptor.chmod(stat.S_IRWXU)
    data = read_config(config)
    data["archive_encryption"] = {
        "argv": [str(encryptor), "--input", "{input}", "--output", "{output}"],
        "verify_argv": [str(encryptor), "--output", "{output}", "--sha256", "{sha256}"],
        "output": str(checkout / "unsafe.enc"),
    }
    write_config_data(config, data)

    assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "unsafe_archive_location")

    assert not (tmp_path / "state").exists()
    assert not (checkout / "unsafe.enc").exists()


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


@pytest.mark.parametrize("single_use", [False, None])
def test_authorization_requires_explicit_single_use_true(tmp_path: Path, single_use: bool | None) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    rewrite_auth(config, sha, "stage", "OOB-123", single_use=single_use)

    assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "authorization_not_single_use")

    assert not (tmp_path / "state").exists()


def test_failed_preflight_does_not_consume_authorization_and_retry_can_succeed(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha, writer_active=True)

    assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "active_writers")
    assert not (tmp_path / "state" / "authorization-consumed.json").exists()

    writers = tmp_path / "writers.json"
    writers.write_text(json.dumps({"ok": True, "active": []}), encoding="utf-8")
    staged = assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))

    assert staged["state"] == "staging-active"
    consumed = json.loads((tmp_path / "state" / "authorization-consumed.json").read_text(encoding="utf-8"))
    assert len(consumed["consumed"]) == 1


def test_authorization_receipt_is_single_use_and_redacted(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)

    assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))
    rewrite_auth(config, sha, "promote", "OOB-123")
    assert_success(cli(config, "promote", sha, "--authorize", "OOB-123"))
    journal = (tmp_path / "state" / "release-journal.jsonl").read_text(encoding="utf-8")
    state = (tmp_path / "state" / "release-state.json").read_text(encoding="utf-8")
    assert "OOB-123" not in journal
    assert "OOB-123" not in state


def test_authorization_hash_is_bound_to_operation(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)

    assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))
    rewrite_auth(config, sha, "promote", "OOB-123")
    promoted = assert_success(cli(config, "promote", sha, "--authorize", "OOB-123"))

    assert promoted["state"] == "promoted"


def test_authorization_digest_is_bound_to_validated_window_and_redacted_everywhere(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    raw_reference = "SECRET-REFERENCE-TOKEN-123"
    config = write_config(tmp_path, checkout, sha, auth_reference=raw_reference)

    first = assert_success(cli(config, "stage", sha, "--authorize", raw_reference))
    first_stdout = json.dumps(first)
    first_consumed = json.loads((tmp_path / "state" / "authorization-consumed.json").read_text(encoding="utf-8"))
    first_proof = first_consumed["operations"][first["operation_id"]]
    assert first_proof["not_before"] == "2026-08-20T12:00:00Z"
    assert first_proof["expires_at"] == "2026-08-20T13:00:00Z"
    assert first_proof["single_use"] is True
    assert_success(cli(config, "rollback"))

    rewrite_auth(
        config,
        sha,
        "stage",
        raw_reference,
        not_before="2026-08-20T12:01:00Z",
        expires_at="2026-08-20T13:01:00Z",
    )
    second = assert_success(cli(config, "stage", sha, "--authorize", raw_reference))
    second_stdout = json.dumps(second)
    consumed = json.loads((tmp_path / "state" / "authorization-consumed.json").read_text(encoding="utf-8"))
    second_proof = consumed["operations"][second["operation_id"]]

    assert first_proof["approval_hash"] != second_proof["approval_hash"]
    assert first_proof["window_hash"] != second_proof["window_hash"]
    assert second_proof["not_before"] == "2026-08-20T12:01:00Z"
    assert second_proof["expires_at"] == "2026-08-20T13:01:00Z"
    assert second_proof["single_use"] is True
    persisted = "\n".join(
        [
            first_stdout,
            second_stdout,
            (tmp_path / "state" / "release-state.json").read_text(encoding="utf-8"),
            (tmp_path / "state" / "release-journal.jsonl").read_text(encoding="utf-8"),
            (tmp_path / "state" / "authorization-consumed.json").read_text(encoding="utf-8"),
        ]
    )
    assert raw_reference not in persisted
    assert "TOKEN-123" not in persisted


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
    "mutation",
    [
        {"source": "/tmp/wrong-source"},
        {"branch": "staging"},
        {"sha": "0" * 40},
        {"service_id": "wrong-service"},
        {"old_pid": 9999},
    ],
)
def test_stage_stopped_probe_must_match_pre_stop_identity_before_switch(tmp_path: Path, mutation: dict[str, object]) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    before_branch = git(checkout, "branch", "--show-current")
    before_head = git(checkout, "rev-parse", "HEAD")
    before_main = git(checkout, "rev-parse", "refs/remotes/origin/main")
    replace_stop_with_stopped_runtime_mutation(config, tmp_path, mutation)

    assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "runtime_identity_mismatch")

    assert git(checkout, "branch", "--show-current") == before_branch
    assert git(checkout, "rev-parse", "HEAD") == before_head
    assert git(checkout, "rev-parse", "refs/remotes/origin/main") == before_main


@pytest.mark.parametrize(
    "mutation",
    [
        {"source": "/tmp/wrong-source"},
        {"branch": "main"},
        {"sha": "0" * 40},
        {"service_id": "wrong-service"},
        {"old_pid": 9999},
    ],
)
def test_promote_stopped_probe_must_match_staged_identity_before_main_switch_or_push(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))
    rewrite_auth(config, sha, "promote", "OOB-456")
    before_branch = git(checkout, "branch", "--show-current")
    before_head = git(checkout, "rev-parse", "HEAD")
    before_main = git(checkout, "rev-parse", "refs/remotes/origin/main")
    replace_stop_with_stopped_runtime_mutation(config, tmp_path, mutation)

    assert_failure(cli(config, "promote", sha, "--authorize", "OOB-456"), "runtime_identity_mismatch")

    assert git(checkout, "branch", "--show-current") == before_branch
    assert git(checkout, "rev-parse", "HEAD") == before_head
    assert git(checkout, "rev-parse", "refs/remotes/origin/main") == before_main


@pytest.mark.parametrize(
    "mutation",
    [
        {"source": "/tmp/wrong-source"},
        {"branch": "main"},
        {"sha": "0" * 40},
        {"service_id": "wrong-service"},
        {"old_pid": 9999},
    ],
)
def test_manual_rollback_stopped_probe_must_match_staged_identity_before_switch(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))
    before_branch = git(checkout, "branch", "--show-current")
    before_head = git(checkout, "rev-parse", "HEAD")
    replace_stop_with_stopped_runtime_mutation(config, tmp_path, mutation)

    assert_failure(cli(config, "rollback"), "runtime_identity_mismatch")

    assert git(checkout, "branch", "--show-current") == before_branch
    assert git(checkout, "rev-parse", "HEAD") == before_head


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


def test_receipts_must_explicitly_cover_every_declared_release_scope(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    scopes = ["repo_local", "config_secrets", "dependencies"]
    config = write_config(tmp_path, checkout, sha, release_scopes=scopes)
    data = read_config(config)
    write_receipt(Path(data["backup_receipt"]), sha=sha, kind="backup", scopes=["repo_local", "config_secrets"])
    write_receipt(Path(data["compatibility_receipt"]), sha=sha, kind="compatibility", scopes=["repo_local"])

    assert_failure(cli(config, "preflight", sha), "receipt_scope_mismatch")


def test_unknown_release_scope_is_rejected(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha, release_scopes=["repo_local", "surprise_scope"])

    assert_failure(cli(config, "preflight", sha), "unknown_release_scope")


def test_compatibility_rollback_sha_must_equal_actual_rollback_sha(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha, release_scopes=["dependencies"])
    data = read_config(config)
    write_receipt(Path(data["compatibility_receipt"]), sha=sha, kind="compatibility", scopes=["dependencies"], rollback_sha=sha)

    assert_failure(cli(config, "preflight", sha), "compatibility_schema_invalid")


@pytest.mark.parametrize(
    "writers_payload",
    [
        {"ok": True, "active": ["writer"]},
        {"ok": True, "active": None},
        {"ok": True},
        {"ok": True, "active": "none"},
    ],
)
def test_writers_probe_must_prove_exactly_empty_active_list(tmp_path: Path, writers_payload: dict[str, object]) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha, writers_payload=writers_payload)

    assert_failure(cli(config, "preflight", sha), "active_writers")


def test_stage_reruns_writers_probe_under_lock_before_stop(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    data = read_config(config)
    writers_probe = tmp_path / "writers-probe.py"
    writers_probe.write_text(
        "import json, pathlib\n"
        f"counter=pathlib.Path({str(tmp_path / 'writers-count')!r})\n"
        "count=int(counter.read_text()) if counter.exists() else 0\n"
        "counter.write_text(str(count + 1))\n"
        "print(json.dumps({'ok': count == 0, 'active': [] if count == 0 else ['late-writer']}))\n",
        encoding="utf-8",
    )
    data["probes"]["writers"] = [sys.executable, str(writers_probe)]  # type: ignore[index]
    write_config_data(config, data)

    assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "active_writers")

    assert not (tmp_path / "lifecycle.log").exists()
    assert git(checkout, "branch", "--show-current") == "main"


def test_mutating_commands_require_configured_service_id(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    data = read_config(config)
    del data["service_id"]
    write_config_data(config, data)

    assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "service_id_required")


def test_preflight_rejects_wrong_configured_service_identity(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha, service_id="expected-service", runtime_service_id="actual-service")

    assert_failure(cli(config, "preflight", sha), "runtime_identity_mismatch")


def test_stage_requires_current_branch_and_head_again_after_stop(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    data = read_config(config)
    drift_hermes = tmp_path / "bin" / "hermes"
    drift_hermes.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        f"printf '%s\\n' \"$*\" >> {str(tmp_path / 'lifecycle.log')!r}\n"
        "if [ \"${*: -2:1}\" = gateway ] && [ \"${*: -1}\" = stop ]; then\n"
        f"  git -C {str(checkout)!r} switch staging >/dev/null\n"
        f"  python3 - <<'PY'\nimport json, pathlib\npath=pathlib.Path({str(tmp_path / 'runtime.json')!r})\ndata=json.loads(path.read_text())\ndata.update({{'ok': True, 'running': False, 'stopped': True, 'old_pid': data.get('pid', data.get('old_pid'))}})\npath.write_text(json.dumps(data))\nPY\n"
        "fi\n",
        encoding="utf-8",
    )
    drift_hermes.chmod(0o700)
    data["lifecycle"]["stop"] = [str(drift_hermes), "gateway", "stop"]  # type: ignore[index]
    write_config_data(config, data)

    assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "branch_cas_failed")

    assert git(checkout, "branch", "--show-current") == "staging"


def test_minimal_code_only_scope_accepts_generic_backup_and_compatibility_receipts(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha, release_scopes=["repo_local"])
    data = json.loads(config.read_text(encoding="utf-8"))
    write_receipt(Path(data["backup_receipt"]), sha=sha, kind="backup", scopes=["repo_local"])
    write_receipt(Path(data["compatibility_receipt"]), sha=sha, kind="compatibility", scopes=["repo_local"])

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
    verifier = tmp_path / "bin" / "verify-archive"
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
    verifier.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib, json, pathlib, sys\n"
        "path = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "sha = sys.argv[sys.argv.index('--sha256') + 1]\n"
        "print(json.dumps({\n"
        "  'ok': True,\n"
        "  'encrypted': True,\n"
        "  'artifact_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),\n"
        "  'operation_id': sys.argv[sys.argv.index('--operation-id') + 1],\n"
        "  'candidate_sha': sys.argv[sys.argv.index('--candidate-sha') + 1],\n"
        "  'rollback_sha': sys.argv[sys.argv.index('--rollback-sha') + 1],\n"
        "}))\n",
        encoding="utf-8",
    )
    verifier.chmod(stat.S_IRWXU)
    data = json.loads(config.read_text(encoding="utf-8"))
    data["archive_encryption"] = {
        "argv": [str(encryptor), "--input", "{input}", "--output", "{output}"],
        "verify_argv": [
            str(verifier),
            "--output",
            "{output}",
            "--sha256",
            "{sha256}",
            "--operation-id",
            "{operation_id}",
            "--candidate-sha",
            "{candidate_sha}",
            "--rollback-sha",
            "{rollback_sha}",
        ],
        "output": str(encrypted),
    }
    config.write_text(json.dumps(data), encoding="utf-8")

    result = assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))

    archive = result["repo_local_archive"]
    assert archive["encrypted"] is True
    assert archive["sha256"]
    assert encrypted.exists()
    assert not Path(archive["plaintext_path"]).exists()


def test_encrypted_archive_output_template_preserves_distinct_operation_outputs(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    (checkout / ".env").write_text("TOKEN=super-secret-token\n", encoding="utf-8")
    config = write_config(tmp_path, checkout, sha)
    encryptor = tmp_path / "bin" / "encrypt-archive"
    verifier = tmp_path / "bin" / "verify-archive"
    encryptor.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, shutil, sys\n"
        "shutil.copyfile(pathlib.Path(sys.argv[sys.argv.index('--input') + 1]), pathlib.Path(sys.argv[sys.argv.index('--output') + 1]))\n",
        encoding="utf-8",
    )
    verifier.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib, json, pathlib, sys\n"
        "path = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "print(json.dumps({\n"
        "  'ok': True,\n"
        "  'encrypted': True,\n"
        "  'artifact_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),\n"
        "  'operation_id': sys.argv[sys.argv.index('--operation-id') + 1],\n"
        "  'candidate_sha': sys.argv[sys.argv.index('--candidate-sha') + 1],\n"
        "  'rollback_sha': sys.argv[sys.argv.index('--rollback-sha') + 1],\n"
        "}))\n",
        encoding="utf-8",
    )
    encryptor.chmod(stat.S_IRWXU)
    verifier.chmod(stat.S_IRWXU)
    data = read_config(config)
    data["archive_encryption"] = {
        "argv": [str(encryptor), "--input", "{input}", "--output", "{output}"],
        "verify_argv": [
            str(verifier),
            "--output",
            "{output}",
            "--sha256",
            "{sha256}",
            "--operation-id",
            "{operation_id}",
            "--candidate-sha",
            "{candidate_sha}",
            "--rollback-sha",
            "{rollback_sha}",
        ],
        "output": str(tmp_path / "encrypted" / "{operation_id}.tar.gz.enc"),
    }
    write_config_data(config, data)

    first = assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))["repo_local_archive"]["archive"]
    assert_success(cli(config, "rollback"))
    rewrite_auth(config, sha, "stage", "OOB-456")
    second = assert_success(cli(config, "stage", sha, "--authorize", "OOB-456"))["repo_local_archive"]["archive"]

    assert first != second
    assert Path(first).exists()
    assert Path(second).exists()


def test_encrypted_archive_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    (checkout / ".env").write_text("TOKEN=super-secret-token\n", encoding="utf-8")
    config = write_config(tmp_path, checkout, sha)
    encryptor = tmp_path / "bin" / "encrypt-archive"
    verifier = tmp_path / "bin" / "verify-archive"
    encrypted = tmp_path / "encrypted.tar.gz.enc"
    encrypted.write_text("existing\n", encoding="utf-8")
    encryptor.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8")
    verifier.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8")
    encryptor.chmod(stat.S_IRWXU)
    verifier.chmod(stat.S_IRWXU)
    data = read_config(config)
    data["archive_encryption"] = {
        "argv": [str(encryptor), "--input", "{input}", "--output", "{output}"],
        "verify_argv": [str(verifier), "--output", "{output}", "--sha256", "{sha256}"],
        "output": str(encrypted),
    }
    write_config_data(config, data)

    assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "archive_output_exists")

    assert encrypted.read_text(encoding="utf-8") == "existing\n"
    assert not any((tmp_path / "state" / "archives").glob("*.tar.gz")) if (tmp_path / "state" / "archives").exists() else True


def test_archive_encryption_timeout_is_structured_and_plaintext_removed(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    (checkout / ".env").write_text("TOKEN=super-secret-token\n", encoding="utf-8")
    config = write_config(tmp_path, checkout, sha)
    slow = tmp_path / "bin" / "slow-encrypt"
    encrypted = tmp_path / "encrypted.tar.gz.enc"
    slow.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(5)\n", encoding="utf-8")
    slow.chmod(stat.S_IRWXU)
    data = read_config(config)
    data["lifecycle"]["timeout_seconds"] = 1  # type: ignore[index]
    data["archive_encryption"] = {
        "argv": [str(slow), "--input", "{input}", "--output", "{output}"],
        "verify_argv": [str(slow), "--output", "{output}", "--sha256", "{sha256}"],
        "output": str(encrypted),
    }
    write_config_data(config, data)

    assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "archive_encryption_failed")

    assert not any((tmp_path / "state" / "archives").glob("*.tar.gz")) if (tmp_path / "state" / "archives").exists() else True


def test_encrypted_archive_requires_verifier_bound_to_output_hash(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    (checkout / ".env").write_text("TOKEN=super-secret-token\n", encoding="utf-8")
    config = write_config(tmp_path, checkout, sha)
    encryptor = tmp_path / "bin" / "encrypt-archive"
    verifier = tmp_path / "bin" / "verify-archive"
    encrypted = tmp_path / "encrypted.tar.gz.enc"
    encryptor.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, shutil, sys\n"
        "shutil.copyfile(pathlib.Path(sys.argv[sys.argv.index('--input') + 1]), pathlib.Path(sys.argv[sys.argv.index('--output') + 1]))\n",
        encoding="utf-8",
    )
    verifier.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'ok': True, 'encrypted': True, 'artifact_sha256': '0' * 64}))\n",
        encoding="utf-8",
    )
    encryptor.chmod(stat.S_IRWXU)
    verifier.chmod(stat.S_IRWXU)
    data = read_config(config)
    data["archive_encryption"] = {
        "argv": [str(encryptor), "--input", "{input}", "--output", "{output}"],
        "verify_argv": [str(verifier), "--output", "{output}", "--sha256", "{sha256}"],
        "output": str(encrypted),
    }
    write_config_data(config, data)

    assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "archive_encryption_verify_failed")

    assert not any((tmp_path / "state" / "archives").glob("*.tar.gz")) if (tmp_path / "state" / "archives").exists() else True


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("operation_id", "stage-wrong"),
        ("candidate_sha", "0" * 40),
        ("rollback_sha", "1" * 40),
        ("artifact_sha256", "2" * 64),
    ],
)
def test_encrypted_archive_verifier_must_echo_operation_and_sha_bindings(
    tmp_path: Path, field: str, wrong_value: str
) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    rollback_sha = str(repo["main_sha"])
    (checkout / ".env").write_text("TOKEN=super-secret-token\n", encoding="utf-8")
    config = write_config(tmp_path, checkout, sha)
    encryptor = tmp_path / "bin" / "encrypt-archive"
    verifier = tmp_path / "bin" / "verify-archive"
    encrypted = tmp_path / "encrypted.tar.gz.enc"
    encryptor.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, shutil, sys\n"
        "shutil.copyfile(pathlib.Path(sys.argv[sys.argv.index('--input') + 1]), pathlib.Path(sys.argv[sys.argv.index('--output') + 1]))\n",
        encoding="utf-8",
    )
    verifier.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib, json, pathlib, sys\n"
        "path = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "actual = hashlib.sha256(path.read_bytes()).hexdigest()\n"
        "proof = {\n"
        "  'ok': True,\n"
        "  'encrypted': True,\n"
        "  'artifact_sha256': actual,\n"
        "  'operation_id': sys.argv[sys.argv.index('--operation-id') + 1],\n"
        "  'candidate_sha': sys.argv[sys.argv.index('--candidate-sha') + 1],\n"
        "  'rollback_sha': sys.argv[sys.argv.index('--rollback-sha') + 1],\n"
        "}\n"
        f"proof[{field!r}] = {wrong_value!r}\n"
        "print(json.dumps(proof))\n",
        encoding="utf-8",
    )
    encryptor.chmod(stat.S_IRWXU)
    verifier.chmod(stat.S_IRWXU)
    data = read_config(config)
    data["archive_encryption"] = {
        "argv": [
            str(encryptor),
            "--input",
            "{input}",
            "--output",
            "{output}",
            "--operation-id",
            "{operation_id}",
            "--candidate-sha",
            "{candidate_sha}",
            "--rollback-sha",
            "{rollback_sha}",
            "--sha256",
            "{sha256}",
        ],
        "verify_argv": [
            str(verifier),
            "--output",
            "{output}",
            "--operation-id",
            "{operation_id}",
            "--candidate-sha",
            "{candidate_sha}",
            "--rollback-sha",
            "{rollback_sha}",
            "--sha256",
            "{sha256}",
        ],
        "output": str(encrypted),
    }
    write_config_data(config, data)

    assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "archive_encryption_verify_failed")

    assert git(checkout, "branch", "--show-current") == "main"
    assert git(checkout, "rev-parse", "HEAD") == rollback_sha
    assert not any((tmp_path / "state" / "archives").glob("*.tar.gz")) if (tmp_path / "state" / "archives").exists() else True


def test_repo_local_inventory_handles_quoted_names_and_rejects_traversal(tmp_path: Path) -> None:
    from hermes_release.controller import _load_config, _inventory_repo_local, _archive_non_reproducible, ReleaseError

    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    tricky = checkout / "space and\nnewline.txt"
    tricky.write_text("fixture\n", encoding="utf-8")
    config = write_config(tmp_path, checkout, sha)
    cfg = _load_config(config)

    inventory = _inventory_repo_local(cfg)

    assert "space and\nnewline.txt" in inventory["non_reproducible"]
    inventory["non_reproducible"].append("../escape")
    with pytest.raises(ReleaseError) as excinfo:
        _archive_non_reproducible(cfg, "unit", inventory, dry_run=False)
    assert excinfo.value.code == "unsafe_repo_local_path"


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


def test_automatic_rollback_requires_stopped_probe_bound_to_failed_staging_identity_before_switching_main(
    tmp_path: Path,
) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    data = read_config(config)
    scripted = tmp_path / "startup-containment" / "hermes"
    counter = tmp_path / "startup-containment" / "stop-count"
    scripted.parent.mkdir()
    scripted.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        f"printf '%s\\n' \"$*\" >> {str(tmp_path / 'lifecycle.log')!r}\n"
        "if [ \"${*: -2:1}\" = gateway ] && [ \"${*: -1}\" = stop ]; then\n"
        "python3 - <<'PY'\n"
        "import json, pathlib\n"
        f"path = pathlib.Path({str(tmp_path / 'runtime.json')!r})\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        "count = int(counter.read_text()) if counter.exists() else 0\n"
        "counter.write_text(str(count + 1))\n"
        "data = json.loads(path.read_text())\n"
        "data.update({'ok': True, 'running': False, 'stopped': True, 'old_pid': data.get('pid', data.get('old_pid'))})\n"
        "if count == 1:\n"
        "    data['old_pid'] = 9999\n"
        "path.write_text(json.dumps(data))\n"
        "PY\n"
        "fi\n"
        "if [ \"${*: -2:1}\" = gateway ] && [ \"${*: -1}\" = start ]; then exit 4; fi\n",
        encoding="utf-8",
    )
    scripted.chmod(0o700)
    data["lifecycle"]["stop"] = [str(scripted), "gateway", "stop"]  # type: ignore[index]
    data["lifecycle"]["start"] = [str(scripted), "gateway", "start"]  # type: ignore[index]
    write_config_data(config, data)

    failed = assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "rollback_uncertain")

    assert failed["state"] == "stopped"
    assert git(checkout, "branch", "--show-current") == "staging"
    assert git(checkout, "rev-parse", "HEAD") == sha


def test_incomplete_stage_stopped_on_main_fails_closed_and_rollback_recovers(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "release-state.json").write_text(
        json.dumps(
            {
                "operation_id": "stage-crash",
                "state": "staging-prepared",
                "phase": "stopped-on-main",
                "candidate_sha": sha,
                "rollback_sha": repo["main_sha"],
                "promoted": False,
                "runtime": {"pid": 1111, "service_id": "hermes-gateway-test"},
                "authorization": {"approval_hash": "stored", "reference_hash": "stored"},
            }
        ),
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "ok": True,
                "running": False,
                "stopped": True,
                "source": str(checkout),
                "branch": "main",
                "sha": repo["main_sha"],
                "service_id": "hermes-gateway-test",
                "old_pid": 1111,
            }
        ),
        encoding="utf-8",
    )

    failed = assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "recovery_required")
    assert failed["state"] == "staging-prepared"
    rolled = assert_success(cli(config, "rollback"))

    assert rolled["state"] == "rolled-back"
    assert git(checkout, "branch", "--show-current") == "main"
    assert git(checkout, "rev-parse", "HEAD") == repo["main_sha"]


def test_incomplete_stage_switched_to_staging_fails_closed_and_rollback_recovers(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    git(checkout, "switch", "staging")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "release-state.json").write_text(
        json.dumps(
            {
                "operation_id": "stage-crash",
                "state": "staging-prepared",
                "phase": "switched-to-staging",
                "candidate_sha": sha,
                "rollback_sha": repo["main_sha"],
                "promoted": False,
                "runtime": {"pid": 1111, "service_id": "hermes-gateway-test"},
                "authorization": {"approval_hash": "stored", "reference_hash": "stored"},
            }
        ),
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "ok": True,
                "running": False,
                "stopped": True,
                "source": str(checkout),
                "branch": "staging",
                "sha": sha,
                "service_id": "hermes-gateway-test",
                "old_pid": 1111,
            }
        ),
        encoding="utf-8",
    )

    failed = assert_failure(cli(config, "stage", sha, "--authorize", "OOB-123"), "recovery_required")
    assert failed["state"] == "staging-prepared"
    rolled = assert_success(cli(config, "rollback"))

    assert rolled["state"] == "rolled-back"
    assert git(checkout, "branch", "--show-current") == "main"


def test_promote_reports_local_main_ready_and_published_phases(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))
    (tmp_path / "smoke.json").write_text(json.dumps({"ok": True, "bad_on_main": {"sha": "0" * 40}}), encoding="utf-8")
    rewrite_auth(config, sha, "promote", "OOB-456")

    failed = assert_failure(cli(config, "promote", sha, "--authorize", "OOB-456"), "promotion_recovery_required")

    assert failed["state"] == "promotion-recovery-required"
    state = json.loads((tmp_path / "state" / "release-state.json").read_text(encoding="utf-8"))
    assert state["phase"] == "startup-failed-after-published-main"
    assert state["promoted"] is True


def test_promote_push_failure_reports_local_main_ready_phase(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))
    hook = Path(str(repo["remote"])) / "hooks" / "pre-receive"
    hook.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    hook.chmod(stat.S_IRWXU)
    rewrite_auth(config, sha, "promote", "OOB-456")

    assert_failure(cli(config, "promote", sha, "--authorize", "OOB-456"), "push_failed")

    status = assert_success(cli(config, "status"))
    assert status["state"] == "promotion-prepared"
    assert status["phase"] == "local-main-ready"
    assert status["promoted"] is False


def test_rollback_start_failure_persists_recovery_required_and_stops_gateway(tmp_path: Path) -> None:
    repo = init_release_repo(tmp_path)
    checkout = repo["checkout"]
    sha = str(repo["staging_sha"])
    config = write_config(tmp_path, checkout, sha)
    assert_success(cli(config, "stage", sha, "--authorize", "OOB-123"))
    data = read_config(config)
    fail_start = tmp_path / "fail-start" / "hermes"
    fail_start.parent.mkdir()
    fail_start.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        f"printf '%s\\n' \"$*\" >> {str(tmp_path / 'lifecycle.log')!r}\n"
        "if [ \"${*: -2:1}\" = gateway ] && [ \"${*: -1}\" = stop ]; then\n"
        f"python3 - <<'PY'\nimport json,pathlib\npath=pathlib.Path({str(tmp_path / 'runtime.json')!r})\ndata=json.loads(path.read_text())\ndata.update({{'ok': True, 'running': False, 'stopped': True, 'old_pid': data.get('pid', data.get('old_pid'))}})\npath.write_text(json.dumps(data))\nPY\n"
        "fi\n"
        "if [ \"${*: -2:1}\" = gateway ] && [ \"${*: -1}\" = start ]; then exit 4; fi\n",
        encoding="utf-8",
    )
    fail_start.chmod(0o700)
    data["lifecycle"]["stop"] = [str(fail_start), "gateway", "stop"]  # type: ignore[index]
    data["lifecycle"]["start"] = [str(fail_start), "gateway", "start"]  # type: ignore[index]
    write_config_data(config, data)

    failed = assert_failure(cli(config, "rollback"), "rollback_recovery_required")

    assert failed["state"] == "rollback-recovery-required"
    state = json.loads((tmp_path / "state" / "release-state.json").read_text(encoding="utf-8"))
    assert state["state"] == "rollback-recovery-required"
    assert state["startup_stop"]["stopped"] is True
    assert json.loads((tmp_path / "runtime.json").read_text(encoding="utf-8"))["stopped"] is True


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
