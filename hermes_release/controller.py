from __future__ import annotations

import argparse
import fcntl
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
ALLOWED_RELEASE_SCOPES = frozenset({"repo_local", "config_secrets", "database_schema", "dependencies"})


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
    service_id: str | None
    current_time: datetime
    reproducible_untracked_globs: tuple[str, ...]
    authorization_receipt: Path | None
    ci_receipt: Path
    review_receipt: Path
    compatibility_receipt: Path
    backup_receipt: Path
    backup_max_age_seconds: int
    forbidden_live_paths: tuple[Path, ...]
    release_scopes: tuple[str, ...]
    archive_encryption: dict[str, Any] | None

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
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ReleaseError(
            "invalid_timestamp",
            "timestamp is not valid ISO-8601",
            "Regenerate the release config or receipt with a valid UTC timestamp, then retry.",
        ) from exc


def _load_config(path: Path) -> Config:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(
            "invalid_config",
            "release config is missing or not valid JSON",
            "Regenerate the release controller config under a disposable path, then retry.",
        ) from exc
    receipts = data
    return Config(
        checkout_path=Path(data["checkout_path"]).resolve(),
        state_dir=Path(data["state_dir"]).resolve(),
        remote=str(data.get("remote", "origin")),
        main_branch=str(data.get("main_branch", "main")),
        staging_branch=str(data.get("staging_branch", "staging")),
        lifecycle=dict(data.get("lifecycle") or {}),
        probes={str(k): list(v) for k, v in (data.get("probes") or {}).items()},
        service_id=str(data["service_id"]) if data.get("service_id") else None,
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
        release_scopes=tuple(str(scope) for scope in data.get("release_scopes", ("repo_local",))),
        archive_encryption=dict(data["archive_encryption"]) if isinstance(data.get("archive_encryption"), dict) else None,
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


def _verify_scoped_backup(data: dict[str, Any], cfg: Config) -> None:
    scopes = set(cfg.release_scopes)
    _verify_receipt_scope(data, cfg, "backup")
    if "config_secrets" in scopes:
        proof = data.get("config_secrets")
        if not isinstance(proof, dict) or proof.get("private") is not True or proof.get("encrypted") is not True:
            raise ReleaseError("backup_schema_invalid", "config/secrets backup proof is incomplete", "Create a private encrypted config/secrets backup receipt with artifact SHA-256 and restore verification, then retry.")
        if not isinstance(proof.get("artifact_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", proof["artifact_sha256"]):
            raise ReleaseError("backup_schema_invalid", "config/secrets backup artifact hash is missing", "Create a private encrypted config/secrets backup receipt with artifact SHA-256 and restore verification, then retry.")
        if proof.get("restore_verified") is not True:
            raise ReleaseError("backup_schema_invalid", "config/secrets restore verification is missing", "Verify config/secrets restore from the encrypted artifact, then retry.")
    if "database_schema" in scopes:
        proof = data.get("database_schema")
        if not isinstance(proof, dict) or proof.get("method") not in {"sqlite-online", "dump"}:
            raise ReleaseError("backup_schema_invalid", "database backup method proof is incomplete", "Create an application-consistent SQLite-online or dump backup receipt, then retry.")
        if proof.get("integrity_ok") is not True or proof.get("encrypted") is not True or proof.get("restore_list_verified") is not True:
            raise ReleaseError("backup_schema_invalid", "database backup verification is incomplete", "Create an encrypted database backup with integrity and restore/list verification, then retry.")
        if not isinstance(proof.get("artifact_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", proof["artifact_sha256"]):
            raise ReleaseError("backup_schema_invalid", "database backup artifact hash is missing", "Create an encrypted database backup receipt with artifact SHA-256, then retry.")


def _verify_scoped_compatibility(data: dict[str, Any], cfg: Config, rollback_sha: str) -> None:
    scopes = set(cfg.release_scopes)
    _verify_receipt_scope(data, cfg, "compatibility")
    if scopes.intersection({"dependencies", "database_schema"}):
        proof = data.get("rollback_compatibility")
        if not isinstance(proof, dict) or proof.get("backward") is not True or proof.get("rollback") is not True:
            raise ReleaseError("compatibility_schema_invalid", "rollback compatibility proof is incomplete", "Create a compatibility receipt proving backward and rollback compatibility to the rollback SHA, then retry.")
        if proof.get("rollback_sha") != rollback_sha:
            raise ReleaseError("compatibility_schema_invalid", "compatibility proof does not bind to rollback SHA", "Regenerate compatibility proof bound to the rollback SHA, then retry.")


def _verify_release_scopes(cfg: Config) -> None:
    unknown = sorted(set(cfg.release_scopes) - ALLOWED_RELEASE_SCOPES)
    if unknown:
        raise ReleaseError(
            "unknown_release_scope",
            "release config declares an unknown release scope",
            "Use only supported release_scopes or extend the controller schema in a reviewed change.",
            details={"unknown_scopes": unknown},
        )


def _verify_receipt_scope(data: dict[str, Any], cfg: Config, label: str) -> None:
    scope = data.get("scope")
    if not isinstance(scope, list) or not all(isinstance(item, str) for item in scope):
        raise ReleaseError(
            "receipt_scope_mismatch",
            f"{label} receipt does not declare its release scope",
            f"Regenerate the {label} receipt with scope covering every configured release_scopes item.",
        )
    missing = sorted(set(cfg.release_scopes) - set(scope))
    unknown = sorted(set(scope) - ALLOWED_RELEASE_SCOPES)
    if missing or unknown:
        raise ReleaseError(
            "receipt_scope_mismatch",
            f"{label} receipt scope does not match configured release scopes",
            f"Regenerate the {label} receipt with explicit scope coverage for the configured release.",
            details={"missing": missing, "unknown": unknown},
        )


def _hash_authorization(reference_id: str, operation: str, sha: str) -> str:
    return hashlib.sha256(f"{sha}\0{operation}\0{reference_id}".encode("utf-8")).hexdigest()


def _consumed_authorizations_path(cfg: Config) -> Path:
    return cfg.state_dir / "authorization-consumed.json"


def _read_consumed_authorizations(cfg: Config) -> dict[str, Any]:
    try:
        data = json.loads(_consumed_authorizations_path(cfg).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"consumed": [], "operations": {}}
    return data if isinstance(data, dict) else {"consumed": [], "operations": {}}


def _verify_authorization(cfg: Config, sha: str, operation: str, reference_id: str | None, *, operation_id: str) -> dict[str, Any]:
    if not reference_id:
        raise ReleaseError("authorization_required", f"{operation} requires explicit operator authorization", f"Retry {operation} with --authorize containing the approved reference id.")
    if cfg.authorization_receipt is None:
        raise ReleaseError("missing_authorization", "authorization receipt is not configured", f"Configure a machine-readable authorization receipt for {operation}, then retry.")
    data = _read_json(cfg.authorization_receipt, "missing_authorization", "authorization")
    if data.get("sha") != sha:
        raise ReleaseError("authorization_sha_mismatch", "authorization receipt does not match candidate SHA", f"Regenerate authorization for {sha}, then retry.")
    if data.get("status") != "ok":
        raise ReleaseError("authorization_not_ok", "authorization receipt is not approved", f"Obtain an approved authorization receipt for {operation}, then retry.")
    if data.get("operation") != operation:
        raise ReleaseError("authorization_operation_mismatch", "authorization receipt operation does not match command", f"Obtain authorization for {operation}, then retry.")
    if data.get("reference_id") != reference_id:
        raise ReleaseError("authorization_id_mismatch", "authorization receipt reference does not match --authorize", f"Retry with the exact approved --authorize reference for {operation}.")
    not_before = data.get("not_before")
    expires_at = data.get("expires_at")
    if not isinstance(not_before, str) or not isinstance(expires_at, str):
        raise ReleaseError("authorization_window_invalid", "authorization receipt window is missing", f"Regenerate authorization with not_before and expires_at for {operation}.")
    if cfg.current_time < _parse_time(not_before):
        raise ReleaseError("authorization_not_yet_valid", "authorization is not yet valid", f"Wait until the authorization window opens, then retry {operation}.")
    if cfg.current_time > _parse_time(expires_at):
        raise ReleaseError("authorization_expired", "authorization has expired", f"Obtain a fresh authorization receipt for {operation}, then retry.")
    if data.get("single_use") is not True:
        raise ReleaseError("authorization_not_single_use", "authorization receipt must be explicitly single-use", f"Obtain a single-use authorization receipt for {operation}, then retry.")
    approval_hash = _hash_authorization(reference_id, operation, sha)
    consumed = _read_consumed_authorizations(cfg)
    if approval_hash in set(consumed.get("consumed") or []):
        raise ReleaseError("authorization_reused", "authorization receipt was already consumed", f"Obtain a fresh single-use authorization receipt for {operation}, then retry.")
    return {
        "approval_hash": approval_hash,
        "reference_hash": hashlib.sha256(reference_id.encode("utf-8")).hexdigest(),
    }


def _consume_authorization(cfg: Config, operation: str, operation_id: str, proof: dict[str, Any]) -> dict[str, Any]:
    approval_hash = proof["approval_hash"]
    consumed = _read_consumed_authorizations(cfg)
    if approval_hash in set(consumed.get("consumed") or []):
        raise ReleaseError("authorization_reused", "authorization receipt was already consumed", f"Obtain a fresh single-use authorization receipt for {operation}, then retry.")
    consumed.setdefault("consumed", []).append(approval_hash)
    consumed.setdefault("operations", {})[operation_id] = dict(proof)
    _atomic_write_json(_consumed_authorizations_path(cfg), consumed)
    return consumed["operations"][operation_id]


def _probe(cfg: Config, name: str) -> dict[str, Any]:
    argv = cfg.probes.get(name)
    if not argv:
        raise ReleaseError(
            "probe_required",
            f"{name} probe is required",
            f"Configure the machine-readable {name} probe, then retry.",
        )
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
    if name == "writers":
        active = data.get("active")
        if not isinstance(active, list) or active:
            raise ReleaseError(
                "active_writers",
                "writers probe did not prove an exactly empty active writer list",
                "Drain all gateway/session writers and retry after the probe emits ok=true with active=[].",
                details={"writers": data},
            )
    return data


def _require_runtime(
    cfg: Config,
    *,
    running: bool,
    branch: str | None = None,
    sha: str | None = None,
    source: Path | None = None,
    service_id: str | None = None,
    old_pid: int | None = None,
    require_new_pid: bool = False,
) -> dict[str, Any]:
    data = _probe(cfg, "runtime")
    if bool(data.get("running")) is not running:
        code = "runtime_not_running" if running else "runtime_not_stopped"
        raise ReleaseError(code, "runtime probe reported unexpected service state", "Use the supported lifecycle path to restore the expected gateway state, then retry.")
    if not running:
        if data.get("stopped") is not True:
            raise ReleaseError("runtime_not_stopped", "runtime probe did not prove the gateway stopped", "Leave the gateway stopped and inspect lifecycle evidence before retrying.")
        return data
    mismatches: dict[str, Any] = {}
    expected_source = str((source or cfg.checkout_path).resolve())
    if data.get("source") != expected_source:
        mismatches["source"] = data.get("source")
    if branch is not None and data.get("branch") != branch:
        mismatches["branch"] = data.get("branch")
    if sha is not None and data.get("sha") != sha:
        mismatches["sha"] = data.get("sha")
    if service_id is not None and data.get("service_id") != service_id:
        mismatches["service_id"] = data.get("service_id")
    pid = data.get("pid")
    if not isinstance(pid, int):
        mismatches["pid"] = pid
    elif require_new_pid and old_pid is not None and pid == old_pid:
        mismatches["pid"] = pid
    if mismatches:
        raise ReleaseError("runtime_identity_mismatch", "runtime identity does not match expected checkout/service", "Leave the gateway stopped and inspect runtime identity before retrying.", details=mismatches)
    return data


def _require_smoke(cfg: Config, *, branch: str, sha: str, source: Path, service_id: str, pid: int) -> dict[str, Any]:
    data = _probe(cfg, "smoke")
    expected = {"source": str(source.resolve()), "branch": branch, "sha": sha, "service_id": service_id, "pid": pid}
    mismatches = {key: data.get(key) for key, value in expected.items() if data.get(key) != value}
    if data.get("target_sha") not in {None, sha}:
        mismatches["target_sha"] = data.get("target_sha")
    if mismatches:
        raise ReleaseError("runtime_identity_mismatch", "smoke identity does not match exact target", "Leave the gateway stopped and inspect smoke/runtime identity before retrying.", details=mismatches)
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


def _path_is_within(path: Path, root: Path) -> bool:
    path = path.resolve()
    root = root.resolve()
    return path == root or root in path.parents


def _ensure_state_outside_checkout(cfg: Config) -> None:
    checkout = cfg.checkout_path.resolve()
    state_dir = cfg.state_dir.resolve()
    archive_dir = cfg.archive_dir.resolve()
    if _path_is_within(state_dir, checkout) or _path_is_within(archive_dir, checkout):
        raise ReleaseError(
            "unsafe_state_location",
            "release state/archive paths must be outside the branch-switched checkout",
            "Move state_dir and archive output outside checkout_path, then retry.",
        )
    encryption = cfg.archive_encryption or {}
    output = encryption.get("output")
    if output:
        output_path = Path(str(output)).resolve()
        if _path_is_within(output_path, checkout) or _path_is_within(output_path, state_dir):
            raise ReleaseError(
                "unsafe_archive_location",
                "archive encryption output must be outside checkout and release state paths",
                "Move archive_encryption.output outside checkout_path and state_dir, then retry.",
            )


def _validate_static_config(cfg: Config) -> None:
    _ensure_not_live(cfg)
    _ensure_state_outside_checkout(cfg)
    _verify_release_scopes(cfg)


def _require_configured_service_id(cfg: Config) -> str:
    if not cfg.service_id:
        raise ReleaseError(
            "service_id_required",
            "release config must declare the expected gateway service identity",
            "Set service_id to the exact supported gateway service identity, then retry.",
        )
    return cfg.service_id


def _git_tree(cfg: Config, rev: str) -> str:
    return _git(cfg, "rev-parse", f"{rev}^{{tree}}")


def _remote_heads(cfg: Config) -> dict[str, str]:
    result = _run(
        ["git", "ls-remote", "--heads", cfg.remote, f"refs/heads/{cfg.main_branch}", f"refs/heads/{cfg.staging_branch}"],
        cwd=cfg.checkout_path,
    )
    if result.returncode != 0:
        raise ReleaseError(
            "remote_ref_unreadable",
            "failed to read authoritative remote refs",
            "Inspect remote connectivity and retry after refs can be read without mutation.",
            details={"stderr": result.stderr.strip()},
        )
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            refs[parts[1].removeprefix("refs/heads/")] = parts[0]
    missing = [branch for branch in (cfg.main_branch, cfg.staging_branch) if branch not in refs]
    if missing:
        raise ReleaseError(
            "remote_ref_unreadable",
            "authoritative remote branch is missing",
            "Restore the required remote branches, then retry.",
            details={"missing": missing},
        )
    return refs


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


def _parse_porcelain_z(raw: str) -> Iterator[tuple[str, str]]:
    records = raw.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        marker = record[:2]
        if marker.startswith("R") or marker.startswith("C"):
            index += 1
        if len(record) >= 4:
            yield marker, record[3:]


def _inventory_repo_local(cfg: Config) -> dict[str, Any]:
    raw = _git(cfg, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored")
    paths: list[str] = []
    ignored: list[str] = []
    reproducible: list[str] = []
    non_reproducible: list[str] = []
    for marker, rel in _parse_porcelain_z(raw):
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


def _validate_repo_local_relpath(rel: str) -> None:
    path = Path(rel)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ReleaseError(
            "unsafe_repo_local_path",
            "repo-local archive path is absolute or escapes the checkout",
            "Remove the unsafe repo-local path from release inventory before retrying.",
        )


def _archive_non_reproducible(cfg: Config, operation_id: str, inventory: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    paths = list(inventory.get("non_reproducible") or [])
    for rel in paths:
        _validate_repo_local_relpath(str(rel))
    if dry_run or not paths:
        return {"paths": paths, "archive": None, "sha256": None, "encrypted": False}
    sensitive = [rel for rel in paths if SECRET_KEY_RE.search(Path(rel).name) or Path(rel).name in {".env", ".netrc"}]
    if sensitive and not cfg.archive_encryption:
        raise ReleaseError(
            "encrypted_archive_required",
            "sensitive repo-local files require encrypted archive configuration",
            "Configure archive_encryption argv/output outside the checkout, then retry.",
            details={"sensitive_paths": ["<redacted sensitive repo-local path>" for _ in sensitive]},
        )
    cfg.archive_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    archive = cfg.archive_dir / f"{operation_id}-repo-local.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for rel in paths:
            source = cfg.checkout_path / rel
            if os.path.lexists(source):
                tar.add(source, arcname=rel, recursive=True)
    archive.chmod(0o600)
    if sensitive:
        encryption = cfg.archive_encryption or {}
        output = Path(str(encryption.get("output") or cfg.archive_dir / f"{operation_id}-repo-local.tar.gz.enc")).resolve()
        argv_template = encryption.get("argv")
        if not isinstance(argv_template, list) or not argv_template:
            archive.unlink(missing_ok=True)
            raise ReleaseError("encrypted_archive_required", "archive encryption argv is missing", "Configure archive_encryption.argv with {input} and {output}, then retry.")
        argv = [str(item).replace("{input}", str(archive)).replace("{output}", str(output)) for item in argv_template]
        try:
            output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            result = _run(argv, timeout=int(cfg.lifecycle.get("timeout_seconds", 60)))
            if result.returncode != 0 or not output.exists():
                raise ReleaseError(
                    "archive_encryption_failed",
                    "repo-local archive encryption failed",
                    "Fix the configured archive encryption command and retry before mutation.",
                    details={"stderr": result.stderr.strip()},
                )
            output.chmod(0o600)
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            verify_template = encryption.get("verify_argv")
            if not isinstance(verify_template, list) or not verify_template:
                raise ReleaseError(
                    "archive_encryption_verify_failed",
                    "archive encryption verifier is missing",
                    "Configure archive_encryption.verify_argv to prove the encrypted artifact hash before mutation.",
                )
            verify_argv = [
                str(item).replace("{input}", str(archive)).replace("{output}", str(output)).replace("{sha256}", digest)
                for item in verify_template
            ]
            verify = _run(verify_argv, timeout=int(cfg.lifecycle.get("timeout_seconds", 60)))
            if verify.returncode != 0:
                raise ReleaseError(
                    "archive_encryption_verify_failed",
                    "archive encryption verifier failed",
                    "Fix archive_encryption.verify_argv so it emits ok=true encrypted=true bound to the output SHA-256.",
                    details={"stderr": verify.stderr.strip()},
                )
            try:
                proof = json.loads(verify.stdout or "{}")
            except json.JSONDecodeError as exc:
                raise ReleaseError(
                    "archive_encryption_verify_failed",
                    "archive encryption verifier did not emit JSON",
                    "Fix archive_encryption.verify_argv so it emits machine-readable JSON.",
                ) from exc
            if proof.get("ok") is not True or proof.get("encrypted") is not True or proof.get("artifact_sha256") != digest:
                raise ReleaseError(
                    "archive_encryption_verify_failed",
                    "archive encryption verifier did not prove the exact encrypted artifact",
                    "Fix archive_encryption.verify_argv so artifact_sha256 equals the output file SHA-256.",
                    details={"verifier": proof},
                )
        finally:
            archive.unlink(missing_ok=True)
        rendered_paths = ["<redacted sensitive repo-local path>" if rel in sensitive else rel for rel in paths]
        return {"paths": rendered_paths, "archive": str(output), "sha256": digest, "encrypted": True, "plaintext_path": str(archive)}
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return {"paths": paths, "archive": str(archive), "sha256": digest, "encrypted": False}


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
    remote = _remote_heads(cfg)
    remote_main = remote[cfg.main_branch]
    local_staging = _local_ref(cfg, cfg.staging_branch)
    remote_staging = remote[cfg.staging_branch]
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
    if require_main_current is None and local_main != remote_main:
        raise ReleaseError(
            "main_ref_drift",
            "local main no longer equals authoritative remote main",
            "Stop; preserve evidence and re-read the authoritative remote before retrying.",
            details={"local_main": local_main, "remote_main": remote_main},
        )
    return {
        "head": head,
        "local_main": local_main,
        "remote_main": remote_main,
        "local_staging": local_staging,
        "remote_staging": remote_staging,
    }


def _require_checkout_position(cfg: Config, branch: str, sha: str, *, code: str = "branch_cas_failed") -> None:
    current_branch = _current_branch(cfg)
    head = _git(cfg, "rev-parse", "HEAD")
    if current_branch != branch or head != sha:
        raise ReleaseError(
            code,
            f"checkout is not on {branch} at the expected SHA",
            "Stop; preserve evidence and restore the checkout to the expected branch and SHA before retrying.",
            details={"branch": current_branch, "head": head},
        )


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
    except json.JSONDecodeError as exc:
        raise ReleaseError(
            "invalid_state",
            "release state is not valid JSON",
            "Preserve the corrupted state file for forensics and recover manually before retrying.",
        ) from exc


@contextmanager
def _lock(cfg: Config) -> Iterator[None]:
    cfg.state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        fd = os.open(cfg.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        try:
            os.close(fd)
        except UnboundLocalError:
            pass
        raise ReleaseError(
            "lock_contended",
            "another release operation holds the global lock",
            "Wait for the active release operation to complete, then retry once.",
        ) from exc
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _manifest(
    cfg: Config,
    sha: str,
    operation_id: str,
    authorization: str | None,
    *,
    dry_run: bool,
    expected_branch: str | None = None,
    expected_head: str | None = None,
    expected_runtime_branch: str | None = None,
    expected_runtime_sha: str | None = None,
) -> dict[str, Any]:
    _validate_static_config(cfg)
    service_id = _require_configured_service_id(cfg)
    _validate_sha(sha)
    _checkout_clean(cfg)
    _worktree_branch_not_elsewhere(cfg, cfg.staging_branch)
    refs = _verify_refs(cfg, sha)
    current_branch = _current_branch(cfg)
    required_branch = expected_branch or cfg.main_branch
    required_head = expected_head or refs["local_main"]
    if current_branch != required_branch or refs["head"] != required_head:
        raise ReleaseError("branch_cas_failed", f"preflight must run from {required_branch} at the expected SHA", f"Switch to {required_branch} at the expected SHA, then retry.")
    inventory = _inventory_repo_local(cfg)
    _verify_receipt(cfg.ci_receipt, sha, "ci", cfg)
    _verify_receipt(cfg.review_receipt, sha, "review", cfg)
    compatibility = _verify_receipt(cfg.compatibility_receipt, sha, "compatibility", cfg)
    backup = _verify_receipt(cfg.backup_receipt, sha, "backup", cfg, max_age=cfg.backup_max_age_seconds)
    _verify_scoped_compatibility(compatibility, cfg, refs["local_main"])
    _verify_scoped_backup(backup, cfg)
    writers = _probe(cfg, "writers")
    runtime = _require_runtime(
        cfg,
        running=True,
        branch=expected_runtime_branch or cfg.main_branch,
        sha=expected_runtime_sha or refs["local_main"],
        source=cfg.checkout_path,
        service_id=service_id,
    )
    return {
        "operation_id": operation_id,
        "authorization": authorization,
        "candidate_sha": sha,
        "candidate_tree": _git_tree(cfg, sha),
        "rollback_sha": refs["local_main"],
        "rollback_tree": _git_tree(cfg, refs["local_main"]),
        "current_branch": current_branch,
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
    manifest = _manifest(cfg, sha, f"dry-run-{sha[:12]}", None, dry_run=True, expected_branch=cfg.main_branch)
    return {
        "ok": True,
        "command": "preflight",
        "dry_run": True,
        "candidate_sha": sha,
        "rollback_sha": manifest["rollback_sha"],
        "inventory": manifest["inventory"],
    }


def _verify_stage_reality(cfg: Config, sha: str, state: dict[str, Any]) -> None:
    if _current_branch(cfg) != cfg.staging_branch:
        raise ReleaseError(
            "stale_state_mismatch",
            "release state claims staging is active but checkout is not on staging",
            f"Run rollback or restage {sha} after restoring checkout reality.",
        )
    _verify_checked_out_tree(cfg, cfg.staging_branch, sha)
    stored_runtime = state.get("runtime") if isinstance(state.get("runtime"), dict) else {}
    stored_smoke = state.get("smoke") if isinstance(state.get("smoke"), dict) else {}
    service_id = _require_configured_service_id(cfg)
    stored_service_id = str(stored_runtime.get("service_id") or stored_smoke.get("service_id") or "")
    if stored_service_id and stored_service_id != service_id:
        raise ReleaseError("runtime_identity_mismatch", "stored runtime service identity does not match configured service", "Inspect release state and configured service_id before retrying.")
    runtime = _require_runtime(
        cfg,
        running=True,
        branch=cfg.staging_branch,
        sha=sha,
        source=cfg.checkout_path,
        service_id=service_id,
    )
    _require_smoke(
        cfg,
        branch=cfg.staging_branch,
        sha=sha,
        source=cfg.checkout_path,
        service_id=str(runtime["service_id"]),
        pid=int(runtime["pid"]),
    )


def stage(cfg: Config, sha: str, authorization: str | None, *, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        return preflight(cfg, sha)
    _validate_static_config(cfg)
    service_id = _require_configured_service_id(cfg)
    operation_id = f"stage-{uuid.uuid4().hex}"
    authorization_proof = _verify_authorization(cfg, sha, "stage", authorization, operation_id=operation_id)
    existing = _read_state(cfg)
    if existing and existing.get("state") == "staging-active" and existing.get("candidate_sha") == sha:
        _verify_stage_reality(cfg, sha, existing)
        return {"ok": True, "command": "stage", "state": "staging-active", "candidate_sha": sha, "operation_id": existing.get("operation_id"), "idempotent": True}
    with _lock(cfg):
        authorization_proof = _verify_authorization(cfg, sha, "stage", authorization, operation_id=operation_id)
        manifest = _manifest(cfg, sha, operation_id, authorization, dry_run=False)
        old_pid = manifest["runtime"].get("pid")
        if not isinstance(old_pid, int):
            raise ReleaseError("runtime_identity_mismatch", "pre-stop runtime pid is missing", "Fix the runtime probe schema, then retry.")
        _require_runtime(cfg, running=True, branch=cfg.main_branch, sha=manifest["rollback_sha"], source=cfg.checkout_path, service_id=service_id)
        if manifest["current_branch"] != cfg.main_branch or manifest["current_head"] != manifest["rollback_sha"]:
            raise ReleaseError("branch_cas_failed", "stage must begin from main at the rollback SHA", "Switch to main at the authoritative rollback SHA, then retry.")
        _probe(cfg, "writers")
        _require_checkout_position(cfg, cfg.main_branch, manifest["rollback_sha"])
        authorization_proof = _consume_authorization(cfg, "stage", operation_id, authorization_proof)
        manifest["authorization"] = authorization_proof
        _append_journal(cfg, {"operation_id": operation_id, "step": "preflight_ok", "manifest": manifest})
        _atomic_write_json(
            cfg.state_path,
            {
                "operation_id": operation_id,
                "state": "staging-prepared",
                "phase": "pre-stop",
                "candidate_sha": sha,
                "rollback_sha": manifest["rollback_sha"],
                "promoted": False,
                "authorization": authorization_proof,
            },
        )
        try:
            _run_lifecycle(cfg, "stop")
            _require_runtime(cfg, running=False)
            _verify_refs(cfg, sha, require_main_current=manifest["rollback_sha"])
            _require_checkout_position(cfg, cfg.main_branch, manifest["rollback_sha"])
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
            runtime = _require_runtime(
                cfg,
                running=True,
                branch=cfg.staging_branch,
                sha=sha,
                source=cfg.checkout_path,
                service_id=service_id,
                old_pid=old_pid,
                require_new_pid=True,
            )
            smoke = _require_smoke(
                cfg,
                branch=cfg.staging_branch,
                sha=sha,
                source=cfg.checkout_path,
                service_id=service_id,
                pid=int(runtime["pid"]),
            )
            _verify_checked_out_tree(cfg, cfg.staging_branch, sha)
        except ReleaseError as exc:
            _append_journal(cfg, {"operation_id": operation_id, "step": "startup_failed", "error": exc.code})
            _attempt_safe_stage_rollback(cfg, manifest, operation_id)
            if exc.code == "runtime_identity_mismatch":
                raise ReleaseError(
                    "runtime_identity_mismatch",
                    "startup or smoke identity did not match the staged target; controller rolled back to main",
                    "Inspect preserved release evidence and fix the candidate runtime identity before restaging.",
                    state="rolled-back",
                ) from exc
            raise ReleaseError(
                "startup_failed_rolled_back",
                "startup or smoke failed; controller rolled back to main",
                "Inspect preserved release evidence and fix the candidate before restaging.",
                state="rolled-back",
            ) from exc
        _atomic_write_json(
            cfg.state_path,
            {
                "operation_id": operation_id,
                "state": "staging-active",
                "phase": "active",
                "candidate_sha": sha,
                "rollback_sha": manifest["rollback_sha"],
                "promoted": False,
                "smoke": smoke,
                "runtime": runtime,
                "authorization": authorization_proof,
            },
        )
        _append_journal(cfg, {"operation_id": operation_id, "step": "staging_active", "smoke": smoke})
        return {
            "ok": True,
            "command": "stage",
            "state": "staging-active",
            "candidate_sha": sha,
            "operation_id": operation_id,
            "repo_local_archive": manifest["repo_local_archive"],
        }


def _attempt_safe_stage_rollback(cfg: Config, manifest: dict[str, Any], operation_id: str) -> None:
    try:
        rollback_safety = _probe(cfg, "rollback_safety")
        refs = _verify_refs(cfg, manifest["candidate_sha"], require_main_current=manifest["rollback_sha"])
        _require_checkout_position(cfg, cfg.staging_branch, manifest["candidate_sha"], code="rollback_uncertain")
        _run_lifecycle(cfg, "stop")
        _require_runtime(cfg, running=False)
    except ReleaseError as exc:
        raise ReleaseError(
            "rollback_uncertain",
            "automatic rollback safety or stopped state could not be proven",
            "Leave the gateway stopped and ask Brian to choose the recovery path.",
            state="stopped",
        ) from exc
    if refs["local_main"] != manifest["rollback_sha"]:
        raise ReleaseError("rollback_uncertain", "rollback SHA drifted", "Leave the gateway stopped and ask Brian to choose the recovery path.", state="stopped")
    result = _run(["git", "switch", cfg.main_branch], cwd=cfg.checkout_path)
    if result.returncode != 0:
        raise ReleaseError("rollback_uncertain", "git switch main failed during automatic rollback", "Leave the gateway stopped and inspect the checkout before retrying.")
    _verify_checked_out_tree(cfg, cfg.main_branch, manifest["rollback_sha"])
    try:
        _run_lifecycle(cfg, "start")
        runtime = _require_runtime(
            cfg,
            running=True,
            branch=cfg.main_branch,
            sha=manifest["rollback_sha"],
            source=cfg.checkout_path,
            service_id=str(manifest["runtime"].get("service_id") or ""),
            old_pid=manifest["runtime"].get("pid"),
            require_new_pid=True,
        )
        smoke = _require_smoke(
            cfg,
            branch=cfg.main_branch,
            sha=manifest["rollback_sha"],
            source=cfg.checkout_path,
            service_id=str(runtime["service_id"]),
            pid=int(runtime["pid"]),
        )
    except ReleaseError as exc:
        raise ReleaseError(
            "rollback_uncertain",
            "automatic rollback could not restart and verify the known-good gateway",
            "Leave the gateway stopped and ask Brian to choose the recovery path.",
            state="stopped",
        ) from exc
    _atomic_write_json(
        cfg.state_path,
        {
            "operation_id": operation_id,
            "state": "rolled-back",
            "candidate_sha": manifest["candidate_sha"],
            "rollback_sha": manifest["rollback_sha"],
            "promoted": False,
            "rollback_safety": rollback_safety,
            "runtime": runtime,
            "smoke": smoke,
        },
    )
    _append_journal(cfg, {"operation_id": operation_id, "step": "automatic_rollback_complete"})


def promote(cfg: Config, sha: str, authorization: str | None, *, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        return preflight(cfg, sha)
    _validate_static_config(cfg)
    service_id = _require_configured_service_id(cfg)
    operation_id = f"promote-{uuid.uuid4().hex}"
    authorization_proof = _verify_authorization(cfg, sha, "promote", authorization, operation_id=operation_id)
    with _lock(cfg):
        state = _read_state(cfg)
        if not state or state.get("state") != "staging-active" or state.get("candidate_sha") != sha:
            raise ReleaseError("no_staged_candidate", "candidate is not active on staging", f"Stage and soak {sha} before promoting it.")
        authorization_proof = _verify_authorization(cfg, sha, "promote", authorization, operation_id=operation_id)
        _verify_stage_reality(cfg, sha, state)
        manifest = _manifest(
            cfg,
            sha,
            operation_id,
            authorization,
            dry_run=False,
            expected_branch=cfg.staging_branch,
            expected_head=sha,
            expected_runtime_branch=cfg.staging_branch,
            expected_runtime_sha=sha,
        )
        if manifest["rollback_sha"] != state.get("rollback_sha"):
            raise ReleaseError("main_ref_drift", "main changed since staging was activated", "Stop; preserve evidence and recover with a normal reviewed commit.")
        _probe(cfg, "writers")
        _require_checkout_position(cfg, cfg.staging_branch, sha)
        authorization_proof = _consume_authorization(cfg, "promote", operation_id, authorization_proof)
        manifest["authorization"] = authorization_proof
        _append_journal(cfg, {"operation_id": operation_id, "step": "promote_preflight_ok", "manifest": manifest})
        _run_lifecycle(cfg, "stop")
        _require_runtime(cfg, running=False)
        _verify_refs(cfg, sha, require_main_current=state["rollback_sha"])
        _require_checkout_position(cfg, cfg.staging_branch, sha)
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
        refs = _verify_refs(cfg, sha)
        if refs["remote_main"] != sha:
            raise ReleaseError("remote_readback_failed", "origin/main did not read back as candidate", "Do not retry blindly; inspect remote refs and preserved promotion evidence.")
        _verify_checked_out_tree(cfg, cfg.main_branch, sha)
        try:
            _run_lifecycle(cfg, "start")
            runtime = _require_runtime(
                cfg,
                running=True,
                branch=cfg.main_branch,
                sha=sha,
                source=cfg.checkout_path,
                service_id=str(manifest["runtime"].get("service_id") or ""),
                old_pid=manifest["runtime"].get("pid"),
                require_new_pid=True,
            )
            smoke = _require_smoke(
                cfg,
                branch=cfg.main_branch,
                sha=sha,
                source=cfg.checkout_path,
                service_id=str(runtime["service_id"]),
                pid=int(runtime["pid"]),
            )
        except ReleaseError as exc:
            stop_status: dict[str, Any]
            try:
                _run_lifecycle(cfg, "stop")
                stopped = _require_runtime(cfg, running=False)
                stop_status = {"stopped": True, "runtime": stopped}
            except ReleaseError as stop_exc:
                stop_status = {
                    "stopped": False,
                    "uncertain": True,
                    "error": stop_exc.code,
                    "recovery_action": "Use supported hermes gateway stop externally, verify stopped, then recover with a normal commit.",
                }
            _atomic_write_json(
                cfg.state_path,
                {
                    "operation_id": operation_id,
                    "state": "promotion-recovery-required",
                    "phase": "startup-failed-after-published-main",
                    "candidate_sha": sha,
                    "rollback_sha": state["rollback_sha"],
                    "promoted": True,
                    "authorization": authorization_proof,
                    "startup_stop": stop_status,
                },
            )
            _append_journal(cfg, {"operation_id": operation_id, "step": "promotion_recovery_required", "error": exc.code})
            raise ReleaseError(
                "promotion_recovery_required",
                "main was published but startup/smoke failed",
                "Create a normal revert or recovery commit from the published main state; do not rewrite main.",
                state="promotion-recovery-required",
            ) from exc
        _atomic_write_json(cfg.state_path, {"operation_id": operation_id, "state": "promoted", "candidate_sha": sha, "rollback_sha": state["rollback_sha"], "promoted": True, "smoke": smoke, "runtime": runtime, "authorization": authorization_proof})
        _append_journal(cfg, {"operation_id": operation_id, "step": "promoted", "smoke": smoke})
        return {"ok": True, "command": "promote", "state": "promoted", "candidate_sha": sha, "operation_id": operation_id}


def rollback(cfg: Config) -> dict[str, Any]:
    _validate_static_config(cfg)
    service_id = _require_configured_service_id(cfg)
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
        _verify_stage_reality(cfg, state["candidate_sha"], state)
        _verify_refs(cfg, state["candidate_sha"], require_main_current=rollback_sha)
        stored_runtime = state.get("runtime") if isinstance(state.get("runtime"), dict) else {}
        old_pid = stored_runtime.get("pid")
        stored_service_id = str(stored_runtime.get("service_id") or "")
        if stored_service_id and stored_service_id != service_id:
            raise ReleaseError("runtime_identity_mismatch", "stored runtime service identity does not match configured service", "Inspect release state and configured service_id before retrying.")
        _run_lifecycle(cfg, "stop")
        _require_runtime(cfg, running=False)
        _verify_refs(cfg, state["candidate_sha"], require_main_current=rollback_sha)
        _require_checkout_position(cfg, cfg.staging_branch, state["candidate_sha"])
        result = _run(["git", "switch", cfg.main_branch], cwd=cfg.checkout_path)
        if result.returncode != 0:
            raise ReleaseError("rollback_uncertain", "git switch main failed", "Leave the gateway stopped and inspect the checkout before retrying.")
        _verify_checked_out_tree(cfg, cfg.main_branch, rollback_sha)
        _run_lifecycle(cfg, "start")
        runtime = _require_runtime(
            cfg,
            running=True,
            branch=cfg.main_branch,
            sha=rollback_sha,
            source=cfg.checkout_path,
            service_id=service_id,
            old_pid=old_pid if isinstance(old_pid, int) else None,
            require_new_pid=True,
        )
        smoke = _require_smoke(cfg, branch=cfg.main_branch, sha=rollback_sha, source=cfg.checkout_path, service_id=str(runtime["service_id"]), pid=int(runtime["pid"]))
        _atomic_write_json(cfg.state_path, {"operation_id": operation_id, "state": "rolled-back", "candidate_sha": state["candidate_sha"], "rollback_sha": rollback_sha, "promoted": False, "smoke": smoke, "runtime": runtime})
        _append_journal(cfg, {"operation_id": operation_id, "step": "rolled_back", "smoke": smoke})
        return {"ok": True, "command": "rollback", "state": "rolled-back", "candidate_sha": state["candidate_sha"], "operation_id": operation_id}


def status(cfg: Config) -> dict[str, Any]:
    _validate_static_config(cfg)
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
