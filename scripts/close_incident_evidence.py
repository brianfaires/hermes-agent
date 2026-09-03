#!/usr/bin/env python3
"""Fail-closed closure utility for one explicit incident evidence directory.

This utility deliberately has no incident discovery, age-based retention, or
Hermes-home default.  It acts only on a caller-supplied directory with a valid
``incident-evidence.json`` manifest and is a dry run unless both ``--apply``
and ``--confirm-closed`` are supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

MANIFEST_NAME = "incident-evidence.json"
SCHEMA_VERSION = 1
REQUIRED_EVIDENCE_KINDS = {"closure_report", "reproducer", "patch", "reference"}
SQLITE_HEADER = b"SQLite format 3\x00"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def _load_manifest(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    if not path.is_file():
        return None, [f"manifest is required: {path.name}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"manifest is unreadable: {exc}"]
    if not isinstance(data, dict):
        return None, ["manifest must be a JSON object"]
    return data, []


def _validate_manifest(incident_dir: Path, manifest: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"unsupported schema_version: {manifest.get('schema_version')!r}")

    incident = manifest.get("incident")
    if not isinstance(incident, dict):
        return [*errors, "incident object is required"], []
    status = incident.get("status")
    if status != "closed":
        errors.append(f"incident must be verified closed; status={status!r}")
    for field in ("closed_at", "closure_report", "retention_policy_version"):
        if not isinstance(incident.get(field), str) or not incident[field].strip():
            errors.append(f"incident.{field} is required")

    closure_report = _safe_relative_path(incident.get("closure_report"))
    if closure_report is not None and not (incident_dir / closure_report).is_file():
        errors.append(f"closure report is missing: {closure_report.as_posix()}")
    elif incident.get("closure_report") and closure_report is None:
        errors.append("incident.closure_report must be a relative path inside the incident directory")

    live_access = manifest.get("live_access")
    if not isinstance(live_access, dict):
        errors.append("live_access object is required")
    else:
        for key, label in (("holders", "holders"), ("leases", "leases")):
            if key not in live_access:
                errors.append(f"live_access.{key} is required")
                continue
            values = live_access[key]
            if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
                errors.append(f"live_access.{key} must be a list of non-empty strings")
            elif values:
                errors.append(f"live access {label} remain: {', '.join(values)}")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        return [*errors, "artifacts must be a list"], []

    normalized: list[dict[str, Any]] = []
    kinds_seen: set[str] = set()
    paths_seen: set[Path] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            errors.append("every artifact must be an object")
            continue
        relative_path = _safe_relative_path(artifact.get("path"))
        kind = artifact.get("kind")
        disposition = artifact.get("disposition", "retain")
        if relative_path is None:
            errors.append("artifact path must be a relative path inside the incident directory")
            continue
        if relative_path in paths_seen:
            errors.append(f"artifact is declared more than once: {relative_path.as_posix()}")
            continue
        paths_seen.add(relative_path)
        if not isinstance(kind, str) or not kind:
            errors.append(f"artifact {relative_path.as_posix()} is missing kind")
            continue
        kinds_seen.add(kind)
        if disposition not in {"retain", "compact", "remove"}:
            errors.append(f"artifact {relative_path.as_posix()} has invalid disposition: {disposition!r}")
        if disposition != "retain" and kind != "full_state_sqlite":
            errors.append(f"only full_state_sqlite artifacts may be compacted or removed: {relative_path.as_posix()}")
        if artifact.get("retain") is True and disposition != "retain":
            errors.append(f"artifact explicitly marked retain cannot be changed: {relative_path.as_posix()}")
        path = incident_dir / relative_path
        if not path.is_file():
            errors.append(f"artifact is missing: {relative_path.as_posix()}")
            continue
        checksum = artifact.get("sha256")
        if not isinstance(checksum, str) or len(checksum) != 64:
            errors.append(f"artifact {relative_path.as_posix()} is missing sha256")
        elif _sha256(path) != checksum.lower():
            errors.append(f"artifact checksum does not match: {relative_path.as_posix()}")
        if kind == "full_state_sqlite" and path.read_bytes()[:16] != SQLITE_HEADER:
            errors.append(f"full_state_sqlite artifact is not SQLite: {relative_path.as_posix()}")
        normalized.append({"path": relative_path, "kind": kind, "disposition": disposition, "artifact": artifact})

    for kind in sorted(REQUIRED_EVIDENCE_KINDS - kinds_seen):
        errors.append(f"required evidence kind is missing: {kind}")
    if closure_report is not None and not any(
        item["kind"] == "closure_report" and item["path"] == closure_report for item in normalized
    ):
        errors.append("closure report must be a checksummed closure_report artifact")
    return errors, normalized


def _unmanifested_sqlite_errors(incident_dir: Path, declared_paths: set[Path]) -> list[str]:
    errors: list[str] = []
    for path in incident_dir.rglob("*"):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        relative_path = path.relative_to(incident_dir)
        if relative_path in declared_paths:
            continue
        try:
            with path.open("rb") as candidate:
                is_sqlite = candidate.read(16) == SQLITE_HEADER
        except OSError as exc:
            errors.append(f"cannot inspect potential snapshot {relative_path.as_posix()}: {exc}")
            continue
        if is_sqlite:
            errors.append(f"unmanifested SQLite snapshot: {relative_path.as_posix()}")
    return errors


def _compact_sqlite(path: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect(path) as source:
            source.execute("VACUUM INTO ?", (str(temporary),))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def close_incident(incident_dir: Path, *, apply: bool = False, confirm_closed: bool = False) -> dict[str, Any]:
    """Validate and optionally close one explicit incident evidence directory."""
    incident_dir = incident_dir.resolve()
    if not incident_dir.is_dir():
        return {"ok": False, "applied": False, "actions": [], "errors": ["incident directory is required"]}
    if apply and not confirm_closed:
        return {"ok": False, "applied": False, "actions": [], "errors": ["--apply requires --confirm-closed"]}

    manifest_path = incident_dir / MANIFEST_NAME
    manifest, errors = _load_manifest(manifest_path)
    if manifest is None:
        return {"ok": False, "applied": False, "actions": [], "errors": errors}
    validation_errors, artifacts = _validate_manifest(incident_dir, manifest)
    errors.extend(validation_errors)
    errors.extend(_unmanifested_sqlite_errors(incident_dir, {item["path"] for item in artifacts}))
    if errors:
        return {"ok": False, "applied": False, "actions": [], "errors": errors}

    targets = [
        item for item in artifacts
        if item["kind"] == "full_state_sqlite" and item["disposition"] in {"compact", "remove"}
    ]
    actions = [{"action": item["disposition"], "path": item["path"].as_posix()} for item in targets]
    if not apply:
        return {"ok": True, "applied": False, "actions": actions, "errors": []}

    for item in targets:
        path = incident_dir / item["path"]
        if item["disposition"] == "compact":
            _compact_sqlite(path)
            item["artifact"]["sha256"] = _sha256(path)
        else:
            path.unlink()
            manifest["artifacts"].remove(item["artifact"])
    _write_manifest(manifest_path, manifest)
    return {"ok": True, "applied": True, "actions": actions, "errors": []}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("incident_dir", type=Path, help="Explicit incident evidence directory; no default is used.")
    parser.add_argument("--apply", action="store_true", help="Perform manifest-declared SQLite compaction/removal.")
    parser.add_argument(
        "--confirm-closed",
        action="store_true",
        help="Required with --apply after independently verifying closure and no live access.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    result = close_incident(args.incident_dir, apply=args.apply, confirm_closed=args.confirm_closed)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
