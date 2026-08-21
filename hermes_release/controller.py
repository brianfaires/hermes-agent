from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SECRET_KEY_RE = re.compile(r"(secret|token|password|passwd|api[_-]?key|authorization|credential)", re.I)
SECRET_VALUE_RE = re.compile(r"(?i)(bearer\s+)?[a-z0-9_=-]*(secret|token|key)[a-z0-9_=-]*")
FORBIDDEN_LIVE_PREFIXES = (
    Path("/home/brian/.hermes/hermes-agent"),
    Path("/home/brian/.hermes/config.yaml"),
)


class ReleaseError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        recovery_action: str,
        *,
        state: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recovery_action = recovery_action
        self.state = state
        self.details = details or {}


@dataclass(frozen=True)
class Config:
    checkout_path: Path
    state_dir: Path
    remote: str
    main_branch: str
    staging_branch: str
    lifecycle: dict[str, Any]
    probes: dict[str, list[str]]
    current_time: datetime
    reproducible_untracked_globs: tuple[str, ...]
    authorization_receipt: Path | None
    ci_receipt: Path
    review_receipt: Path
    compatibility_receipt: Path
    backup_receipt: Path
    backup_max_age_seconds: int
    forbidden_live_paths: tuple[Path, ...]

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "release.lock"

    @property
    def journal_path(self) -> Path:
        return self.state_dir / "release-journal.jsonl"

    @property
    def state_path(self) -> Path:
        return self.state_dir / "release-state.json"

    @property
    def archive_dir(self) -> Path:
        return self.state_dir / "archives"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_config(path: Path) -> Config:
    data = json.loads(path.read_text(encoding="utf-8"))
    receipts = data
    return Config(
        checkout_path=Path(data["checkout_path"]).resolve(),
        state_dir=Path(data["state_dir"]).resolve(),
        remote=str(data.get("remote", "origin")),
        main_branch=str(data.get("main_branch", "main")),
        staging_branch=str(data.get("staging_branch", "staging")),
        lifecycle=dict(data.get("lifecycle") or {}),
        probes={str(k): list(v) for k, v in (data.get("probes") or {}).items()},
        current_time=_parse_time(str(data["current_time"])) if data.get("current_time") else _utc_now(),
        reproducible_untracked_globs=tuple(data.get("reproducible_untracked_globs") or ()),
        authorization_receipt=Path(receipts["authorization_receipt"]).resolve()
        if receipts.get("authorization_receipt")
        else None,
        ci_receipt=Path(receipts["ci_receipt"]).resolve(),
        review_receipt=Path(receipts["review_receipt"]).resolve(),
        compatibility_receipt=Path(receipts["compatibility_receipt"]).resolve(),
        backup_receipt=Path(receipts["backup_receipt"]).resolve(),
        backup_max_age_seconds=int(data.get("backup_max_age_seconds", 3600)),
        forbidden_live_paths=tuple(Path(p).resolve() for p in data.get("forbidden_live_paths", ())),
    )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        if SECRET_VALUE_RE.search(value):
            return "<redacted>"
        return value
    return value


def _emit(payload: dict[str, Any], returncode: int) -> int:
    print(json.dumps(_redact(payload), sort_keys=True))
    return returncode


def _fail(error: ReleaseError, command: str) -> int:
    payload = {
        "ok": False,
        "command": command,
        "code": error.code,
        "message": error.message,
        "recovery_action": error.recovery_action,
    }
    if error.state:
        payload["state"] = error.state
    if error.details:
        payload["details"] = error.details
    return _emit(payload, 2)


def _validate_sha(sha: str) -> None:
    if not SHA_RE.fullmatch(sha):
        raise ReleaseError(
            "invalid_sha",
            "candidate must be an exact lowercase 40-hex SHA",
            "Re-run the command with the exact 40-hex candidate SHA.",
        )


def _run(argv: list[str], *, cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)


def _git(cfg: Config, *args: str, check: bool = True) -> str:
    result = _run(["git", *args], cwd=cfg.checkout_path)
    if check and result.returncode != 0:
        raise ReleaseError(
            "git_failed",
            f"git {' '.join(args)} failed",
            "Preserve the release state directory and inspect the recorded git failure before retrying.",
            details={"stderr": result.stderr.strip(), "stdout": result.stdout.strip()},
        )
    return result.stdout.strip()


def _read_json(path: Path, code: str, label: str) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReleaseError(
            code,
            f"{label} receipt is missing",
            f"Create a fresh machine-readable {label} receipt for the exact candidate SHA, then retry.",
        ) from exc
    except json.JSONDecodeError as exc:
        raise ReleaseError(
            code,
            f"{label} receipt is not valid JSON",
            f"Replace the {label} receipt with valid machine-readable evidence, then retry.",
        ) from exc


def _verify_receipt(path: Path, sha: str, label: str, cfg: Config, *, max_age: int | None = None) -> dict[str, Any]:
    data = _read_json(path, "missing_receipt", label)
    if data.get("sha") != sha:
        raise ReleaseError(
            "receipt_sha_mismatch",
            f"{label} receipt does not match candidate SHA",
            f"Regenerate the {label} receipt for {sha}, then retry.",
        )
    if data.get("status") != "ok":
        raise ReleaseError(
            "receipt_not_ok",
            f"{label} receipt is not green",
            f"Resolve the failing {label} receipt and retry with status=ok.",
        )
    if max_age is not None:
        issued = data.get("issued_at")
        if not isinstance(issued, str):
            raise ReleaseError("stale_receipt", f"{label} receipt has no issued_at", f"Regenerate the {label} receipt, then retry.")
        age = (cfg.current_time - _parse_time(issued)).total_seconds()
        if age < 0 or age > max_age:
            raise ReleaseError(
                "stale_receipt",
                f"{label} receipt is stale",
                f"Create a fresh {label} receipt inside the release window, then retry.",
            )
    return data


def _probe(cfg: Config, name: str) -> dict[str, Any]:
    argv = cfg.probes.get(name)
    if not argv:
        return {"ok": True, "skipped": True}
    try:
        result = _run(argv, timeout=int(cfg.lifecycle.get("timeout_seconds", 30)))
    except subprocess.TimeoutExpired as exc:
        raise ReleaseError(
            "probe_failed",
            f"{name} probe timed out",
            f"Fix the {name} probe or its subject, then retry.",
        ) from exc
    if result.returncode != 0:
        raise ReleaseError(
            "probe_failed",
            f"{name} probe failed",
            f"Fix the {name} probe failure, then retry.",
            details={"stderr": result.stderr.strip()},
        )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ReleaseError(
            "probe_failed",
            f"{name} probe did not emit JSON",
            f"Fix the {name} probe to emit a machine-readable JSON contract, then retry.",
        ) from exc
    if data.get("ok") is not True:
        code = "active_writers" if name == "writers" else "probe_failed"
        raise ReleaseError(
            code,
            f"{name} probe reported an unsafe state",
            f"Resolve the {name} probe finding, then retry.",
            details={name: data},
        )
    return data


def _ensure_not_live(cfg: Config) -> None:
    targets = set(FORBIDDEN_LIVE_PREFIXES) | set(cfg.forbidden_live_paths)
    checkout = cfg.checkout_path.resolve()
    for forbidden in targets:
        forbidden = forbidden.resolve()
        if checkout == forbidden or forbidden in checkout.parents:
            raise ReleaseError(
                "live_system_guard",
                "configured checkout points at a protected live Hermes path",
                "Use a temporary fixture checkout for tests, or run the approved production controller outside this inactive candidate.",
            )


def _git_tree(cfg: Config, rev: str) -> str:
    return _git(cfg, "rev-parse", f"{rev}^{{tree}}")


def _remote_ref(cfg: Config, branch: str) -> str:
    return _git(cfg, "rev-parse", f"refs/remotes/{cfg.remote}/{branch}")


def _local_ref(cfg: Config, branch: str) -> str:
    return _git(cfg, "rev-parse", f"refs/heads/{branch}")


def _current_branch(cfg: Config) -> str:
    return _git(cfg, "branch", "--show-current")


def _checkout_clean(cfg: Config) -> None:
    status = _git(cfg, "status", "--porcelain=v1", "--untracked-files=no")
    if status:
        raise ReleaseError(
            "dirty_checkout",
            "checkout has uncommitted or untracked paths",
            "Commit, remove, or explicitly classify the checkout changes before retrying.",
            details={"status": status},
        )
    diff = _run(["git", "diff-index", "--quiet", "HEAD", "--"], cwd=cfg.checkout_path)
    cached = _run(["git", "diff", "--cached", "--quiet"], cwd=cfg.checkout_path)
    if diff.returncode != 0 or cached.returncode != 0:
        raise ReleaseError("dirty_checkout", "tracked worktree or index is dirty", "Restore a clean tracked checkout, then retry.")


def _inventory_repo_local(cfg: Config) -> dict[str, Any]:
    raw = _git(cfg, "status", "--porcelain=v1", "--untracked-files=all", "--ignored")
    paths: list[str] = []
    ignored: list[str] = []
    reproducible: list[str] = []
    non_reproducible: list[str] = []
    for line in raw.splitlines():
        if not line:
            continue
        marker, rel = line[:2], line[3:]
        if marker == "!!":
            ignored.append(rel)
        elif marker == "??":
            paths.append(rel)
        else:
            continue
        if any(fnmatch.fnmatch(rel, pattern) for pattern in cfg.reproducible_untracked_globs):
            reproducible.append(rel)
        else:
            non_reproducible.append(rel)
    return {
        "untracked": paths,
        "ignored": ignored,
        "reproducible_exclusions": reproducible,
        "non_reproducible": non_reproducible,
        "sensitive_path_markers": [
            "<redacted sensitive repo-local path>"
            for rel in [*paths, *ignored]
            if SECRET_KEY_RE.search(Path(rel).name) or Path(rel).name in {".env", ".netrc"}
        ],
    }


def _archive_non_reproducible(cfg: Config, operation_id: str, inventory: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    paths = list(inventory.get("non_reproducible") or [])
    if dry_run or not paths:
        return {"paths": paths, "archive": None, "sha256": None}
    cfg.archive_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    archive = cfg.archive_dir / f"{operation_id}-repo-local.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for rel in paths:
            source = cfg.checkout_path / rel
            if source.exists():
                tar.add(source, arcname=rel, recursive=True)
    archive.chmod(0o600)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return {"paths": paths, "archive": str(archive), "sha256": digest}


def _worktree_branch_not_elsewhere(cfg: Config, branch: str) -> None:
    output = _git(cfg, "worktree", "list", "--porcelain")
    current_path: Path | None = None
    for line in output.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree ")).resolve()
        elif line == f"branch refs/heads/{branch}" and current_path and current_path != cfg.checkout_path:
            raise ReleaseError(
                "branch_checked_out_elsewhere",
                f"{branch} is checked out in another worktree",
                f"Remove or switch the other {branch} worktree, then retry.",
                details={"worktree": str(current_path)},
            )


def _verify_refs(cfg: Config, sha: str, *, require_main_current: str | None = None) -> dict[str, str]:
    head = _git(cfg, "rev-parse", "HEAD")
    local_main = _local_ref(cfg, cfg.main_branch)
    remote_main = _remote_ref(cfg, cfg.main_branch)
    local_staging = _local_ref(cfg, cfg.staging_branch)
    remote_staging = _remote_ref(cfg, cfg.staging_branch)
    if remote_staging != sha or local_staging != sha:
        raise ReleaseError(
            "sha_drift",
            "staging ref no longer equals the approved candidate",
            f"Requalify the current staging ref and retry with its exact SHA.",
        )
    if require_main_current and (local_main != require_main_current or remote_main != require_main_current):
        raise ReleaseError(
            "main_ref_drift",
            "main no longer equals the preserved rollback SHA",
            "Stop; preserve evidence and recover with a normal reviewed commit rather than rewriting main.",
        )
    return {
        "head": head,
        "local_main": local_main,
        "remote_main": remote_main,
        "local_staging": local_staging,
        "remote_staging": remote_staging,
    }


def _validate_lifecycle_argv(argv: list[str], action: str) -> None:
    if not argv or Path(argv[0]).name != "hermes":
        raise ReleaseError(
            "invalid_lifecycle",
            "mutating lifecycle command must invoke hermes",
            "Configure lifecycle argv as an external hermes gateway stop/start command.",
        )
    if len(argv) < 3 or argv[-2:] != ["gateway", action]:
        raise ReleaseError(
            "invalid_lifecycle",
            "mutating lifecycle command must be hermes ... gateway stop/start",
            "Configure lifecycle argv as an external hermes gateway stop/start command.",
        )


def _run_lifecycle(cfg: Config, action: str) -> dict[str, Any]:
    argv = list(cfg.lifecycle.get(action) or [])
    _validate_lifecycle_argv(argv, action)
    try:
        result = _run(argv, timeout=int(cfg.lifecycle.get("timeout_seconds", 60)))
    except subprocess.TimeoutExpired as exc:
        raise ReleaseError(
            "lifecycle_failed",
            f"gateway {action} command timed out",
            f"From an external shell, inspect the fake/real gateway lifecycle state before retrying {action}.",
        ) from exc
    if result.returncode != 0:
        raise ReleaseError(
            "lifecycle_failed",
            f"gateway {action} command failed",
            f"From an external shell, inspect gateway {action} output and preserve release evidence before retrying.",
            details={"stderr": result.stderr.strip(), "stdout": result.stdout.strip(), "returncode": result.returncode},
        )
    return {"argv": argv, "stdout": result.stdout.strip()}


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(_redact(payload), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _fsync_dir(path.parent)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _append_journal(cfg: Config, event: dict[str, Any]) -> None:
    cfg.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    line = json.dumps(_redact(event), sort_keys=True) + "\n"
    fd = os.open(cfg.journal_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_state(cfg: Config) -> dict[str, Any] | None:
    try:
        return json.loads(cfg.state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


@contextmanager
def _lock(cfg: Config) -> Iterator[None]:
    cfg.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        fd = os.open(cfg.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ReleaseError(
            "lock_contended",
            "another release operation holds the global lock",
            "Wait for the active release operation to complete, or inspect the state directory if it crashed.",
        ) from exc
    try:
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
        yield
    finally:
        os.close(fd)
        try:
            cfg.lock_path.unlink()
        except FileNotFoundError:
            pass


def _manifest(cfg: Config, sha: str, operation_id: str, authorization: str | None, *, dry_run: bool) -> dict[str, Any]:
    _ensure_not_live(cfg)
    _validate_sha(sha)
    _checkout_clean(cfg)
    _worktree_branch_not_elsewhere(cfg, cfg.staging_branch)
    refs = _verify_refs(cfg, sha)
    inventory = _inventory_repo_local(cfg)
    _verify_receipt(cfg.ci_receipt, sha, "ci", cfg)
    _verify_receipt(cfg.review_receipt, sha, "review", cfg)
    _verify_receipt(cfg.compatibility_receipt, sha, "compatibility", cfg)
    _verify_receipt(cfg.backup_receipt, sha, "backup", cfg, max_age=cfg.backup_max_age_seconds)
    if cfg.authorization_receipt is not None:
        _verify_receipt(cfg.authorization_receipt, sha, "authorization", cfg)
    writers = _probe(cfg, "writers")
    runtime = _probe(cfg, "runtime")
    return {
        "operation_id": operation_id,
        "authorization": authorization,
        "candidate_sha": sha,
        "candidate_tree": _git_tree(cfg, sha),
        "rollback_sha": refs["local_main"],
        "rollback_tree": _git_tree(cfg, refs["local_main"]),
        "current_branch": _current_branch(cfg),
        "current_head": refs["head"],
        "refs": refs,
        "inventory": inventory,
        "repo_local_archive": _archive_non_reproducible(cfg, operation_id, inventory, dry_run=dry_run),
        "runtime": runtime,
        "writers": writers,
    }


def _verify_checked_out_tree(cfg: Config, branch: str, sha: str) -> None:
    head = _git(cfg, "rev-parse", "HEAD")
    local = _local_ref(cfg, branch)
    index_tree = _git(cfg, "write-tree")
    expected_tree = _git_tree(cfg, sha)
    diff = _run(["git", "diff", "--quiet"], cwd=cfg.checkout_path)
    cached = _run(["git", "diff", "--cached", "--quiet"], cwd=cfg.checkout_path)
    if head != sha or local != sha or index_tree != expected_tree or diff.returncode != 0 or cached.returncode != 0:
        raise ReleaseError(
            "post_switch_mismatch",
            "post-switch HEAD/ref/index/working bytes do not match expected tree",
            "Keep the gateway stopped, preserve evidence, and inspect the checkout mismatch.",
        )


def preflight(cfg: Config, sha: str) -> dict[str, Any]:
    manifest = _manifest(cfg, sha, f"dry-run-{sha[:12]}", None, dry_run=True)
    return {
        "ok": True,
        "command": "preflight",
        "dry_run": True,
        "candidate_sha": sha,
        "rollback_sha": manifest["rollback_sha"],
        "inventory": manifest["inventory"],
    }


def stage(cfg: Config, sha: str, authorization: str | None, *, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        return preflight(cfg, sha)
    if not authorization:
        raise ReleaseError("authorization_required", "stage requires explicit operator authorization", "Retry with --authorize containing the approved release-window token.")
    existing = _read_state(cfg)
    if existing and existing.get("state") == "staging-active" and existing.get("candidate_sha") == sha:
        return {"ok": True, "command": "stage", "state": "staging-active", "candidate_sha": sha, "operation_id": existing.get("operation_id"), "idempotent": True}
    with _lock(cfg):
        operation_id = f"stage-{uuid.uuid4().hex}"
        manifest = _manifest(cfg, sha, operation_id, authorization, dry_run=False)
        _append_journal(cfg, {"operation_id": operation_id, "step": "preflight_ok", "manifest": manifest})
        _atomic_write_json(cfg.state_path, {"operation_id": operation_id, "state": "staging-prepared", "candidate_sha": sha, "rollback_sha": manifest["rollback_sha"], "promoted": False})
        try:
            _run_lifecycle(cfg, "stop")
            _append_journal(cfg, {"operation_id": operation_id, "step": "gateway_stopped"})
        except ReleaseError as exc:
            _append_journal(cfg, {"operation_id": operation_id, "step": "stop_gateway_failed", "error": exc.code})
            raise
        stopped_branch = _current_branch(cfg)
        try:
            result = _run(["git", "switch", cfg.staging_branch], cwd=cfg.checkout_path)
            if result.returncode != 0:
                raise ReleaseError(
                    "switch_failed",
                    "git switch staging failed",
                    "Keep the gateway stopped, preserve the release state directory, and inspect the switch failure.",
                    details={"stderr": result.stderr.strip(), "stdout": result.stdout.strip()},
                )
            _verify_checked_out_tree(cfg, cfg.staging_branch, sha)
            _append_journal(cfg, {"operation_id": operation_id, "step": "switched_to_staging"})
        except ReleaseError:
            _append_journal(cfg, {"operation_id": operation_id, "step": "switch_failed", "from_branch": stopped_branch})
            raise
        try:
            _run_lifecycle(cfg, "start")
            smoke = _probe(cfg, "smoke")
        except ReleaseError as exc:
            _append_journal(cfg, {"operation_id": operation_id, "step": "startup_failed", "error": exc.code})
            _attempt_safe_stage_rollback(cfg, manifest, operation_id)
            raise ReleaseError(
                "startup_failed_rolled_back",
                "startup or smoke failed; controller rolled back to main",
                "Inspect preserved release evidence and fix the candidate before restaging.",
                state="rolled-back",
            ) from exc
        _atomic_write_json(cfg.state_path, {"operation_id": operation_id, "state": "staging-active", "candidate_sha": sha, "rollback_sha": manifest["rollback_sha"], "promoted": False, "smoke": smoke})
        _append_journal(cfg, {"operation_id": operation_id, "step": "staging_active", "smoke": smoke})
        return {"ok": True, "command": "stage", "state": "staging-active", "candidate_sha": sha, "operation_id": operation_id}


def _attempt_safe_stage_rollback(cfg: Config, manifest: dict[str, Any], operation_id: str) -> None:
    try:
        rollback_safety = _probe(cfg, "rollback_safety")
        refs = _verify_refs(cfg, manifest["candidate_sha"], require_main_current=manifest["rollback_sha"])
    except ReleaseError as exc:
        raise ReleaseError(
            "rollback_uncertain",
            "automatic rollback safety could not be proven",
            "Leave the gateway stopped and ask Brian to choose the recovery path.",
            state="stopped",
        ) from exc
    if refs["local_main"] != manifest["rollback_sha"]:
        raise ReleaseError("rollback_uncertain", "rollback SHA drifted", "Leave the gateway stopped and ask Brian to choose the recovery path.", state="stopped")
    result = _run(["git", "switch", cfg.main_branch], cwd=cfg.checkout_path)
    if result.returncode != 0:
        raise ReleaseError("rollback_uncertain", "git switch main failed during automatic rollback", "Leave the gateway stopped and inspect the checkout before retrying.")
    _verify_checked_out_tree(cfg, cfg.main_branch, manifest["rollback_sha"])
    _atomic_write_json(cfg.state_path, {"operation_id": operation_id, "state": "rolled-back", "candidate_sha": manifest["candidate_sha"], "rollback_sha": manifest["rollback_sha"], "promoted": False, "rollback_safety": rollback_safety})
    _append_journal(cfg, {"operation_id": operation_id, "step": "automatic_rollback_complete"})


def promote(cfg: Config, sha: str, authorization: str | None, *, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        return preflight(cfg, sha)
    if not authorization:
        raise ReleaseError("authorization_required", "promote requires explicit operator authorization", "Retry with --authorize containing the approved promotion token.")
    with _lock(cfg):
        state = _read_state(cfg)
        if not state or state.get("state") != "staging-active" or state.get("candidate_sha") != sha:
            raise ReleaseError("no_staged_candidate", "candidate is not active on staging", f"Stage and soak {sha} before promoting it.")
        operation_id = f"promote-{uuid.uuid4().hex}"
        manifest = _manifest(cfg, sha, operation_id, authorization, dry_run=False)
        if manifest["rollback_sha"] != state.get("rollback_sha"):
            raise ReleaseError("main_ref_drift", "main changed since staging was activated", "Stop; preserve evidence and recover with a normal reviewed commit.")
        _append_journal(cfg, {"operation_id": operation_id, "step": "promote_preflight_ok", "manifest": manifest})
        _run_lifecycle(cfg, "stop")
        switch = _run(["git", "switch", cfg.main_branch], cwd=cfg.checkout_path)
        if switch.returncode != 0:
            raise ReleaseError("switch_failed", "git switch main failed", "Keep the gateway stopped and inspect the checkout before retrying.", details={"stderr": switch.stderr.strip()})
        _verify_refs(cfg, sha, require_main_current=state["rollback_sha"])
        merge = _run(["git", "merge", "--ff-only", cfg.staging_branch], cwd=cfg.checkout_path)
        if merge.returncode != 0:
            raise ReleaseError("merge_failed", "git merge --ff-only staging failed", "Do not force main; inspect divergence and requalify the release.", details={"stderr": merge.stderr.strip()})
        push = _run(["git", "push", cfg.remote, cfg.main_branch], cwd=cfg.checkout_path)
        if push.returncode != 0:
            raise ReleaseError("push_failed", "git push main failed", "Preserve evidence and inspect the remote before retrying promotion.", details={"stderr": push.stderr.strip()})
        _git(cfg, "fetch", cfg.remote, cfg.main_branch, cfg.staging_branch)
        refs = _verify_refs(cfg, sha)
        if refs["remote_main"] != sha:
            raise ReleaseError("remote_readback_failed", "origin/main did not read back as candidate", "Do not retry blindly; inspect remote refs and preserved promotion evidence.")
        _verify_checked_out_tree(cfg, cfg.main_branch, sha)
        _run_lifecycle(cfg, "start")
        smoke = _probe(cfg, "smoke")
        _atomic_write_json(cfg.state_path, {"operation_id": operation_id, "state": "promoted", "candidate_sha": sha, "rollback_sha": state["rollback_sha"], "promoted": True, "smoke": smoke})
        _append_journal(cfg, {"operation_id": operation_id, "step": "promoted", "smoke": smoke})
        return {"ok": True, "command": "promote", "state": "promoted", "candidate_sha": sha, "operation_id": operation_id}


def rollback(cfg: Config) -> dict[str, Any]:
    with _lock(cfg):
        state = _read_state(cfg)
        if not state:
            raise ReleaseError("no_release_state", "no release state exists", "Inspect the checkout manually; there is no controller rollback state to apply.")
        if state.get("promoted") or state.get("state") == "promoted":
            raise ReleaseError(
                "post_promotion_rollback_refused",
                "published main cannot be rewritten by rollback",
                "Create a normal revert or recovery commit, qualify it, and promote that commit without force-pushing.",
            )
        rollback_sha = state.get("rollback_sha")
        if not isinstance(rollback_sha, str) or not SHA_RE.fullmatch(rollback_sha):
            raise ReleaseError("rollback_uncertain", "rollback identity is missing or invalid", "Leave the gateway stopped and ask Brian to choose the recovery path.")
        operation_id = f"rollback-{uuid.uuid4().hex}"
        _verify_refs(cfg, state["candidate_sha"], require_main_current=rollback_sha)
        _run_lifecycle(cfg, "stop")
        result = _run(["git", "switch", cfg.main_branch], cwd=cfg.checkout_path)
        if result.returncode != 0:
            raise ReleaseError("rollback_uncertain", "git switch main failed", "Leave the gateway stopped and inspect the checkout before retrying.")
        _verify_checked_out_tree(cfg, cfg.main_branch, rollback_sha)
        _run_lifecycle(cfg, "start")
        smoke = _probe(cfg, "smoke")
        _atomic_write_json(cfg.state_path, {"operation_id": operation_id, "state": "rolled-back", "candidate_sha": state["candidate_sha"], "rollback_sha": rollback_sha, "promoted": False, "smoke": smoke})
        _append_journal(cfg, {"operation_id": operation_id, "step": "rolled_back", "smoke": smoke})
        return {"ok": True, "command": "rollback", "state": "rolled-back", "candidate_sha": state["candidate_sha"], "operation_id": operation_id}


def status(cfg: Config) -> dict[str, Any]:
    _ensure_not_live(cfg)
    state = _read_state(cfg) or {"state": "inactive"}
    payload = {"ok": True, "command": "status", **state}
    try:
        payload["checkout_branch"] = _current_branch(cfg)
        payload["checkout_head"] = _git(cfg, "rev-parse", "HEAD")
    except ReleaseError as exc:
        payload["checkout_error"] = exc.code
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-release")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "stage", "promote"):
        p = sub.add_parser(name)
        p.add_argument("sha")
        p.add_argument("--authorize")
    sub.add_parser("rollback")
    sub.add_parser("status")
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = str(args.command)
    try:
        cfg = _load_config(args.config)
        if command == "preflight":
            result = preflight(cfg, args.sha)
        elif command == "stage":
            result = stage(cfg, args.sha, args.authorize, dry_run=args.dry_run)
        elif command == "promote":
            result = promote(cfg, args.sha, args.authorize, dry_run=args.dry_run)
        elif command == "rollback":
            result = rollback(cfg)
        else:
            result = status(cfg)
        return _emit(result, 0)
    except ReleaseError as exc:
        return _fail(exc, command)


def main(argv: list[str] | None = None) -> int:
    return run_cli(argv)
