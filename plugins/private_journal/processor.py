"""Nightly one-batch processor for private journal holding records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from hermes_cli.config import cfg_get, load_config

from .capture import PrivateJournalStoreError, holding_dir, validate_entry_id

try:  # POSIX advisory locking
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised with a simulated Windows backend
    fcntl = None

try:  # Windows advisory locking
    import msvcrt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - POSIX does not provide msvcrt
    msvcrt = None

DEFAULT_MAX_BATCH_BYTES = 96_000
DEFAULT_MAX_BATCH_COUNT = 100
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 120
TASK_NAME = "private_journal_batch"
RAW_SECTION_START_RE = re.compile(
    rb"<!-- private-journal-raw-v1 bytes=(\d+) sha256=([0-9a-f]{64}) -->\n"
)

ALLOWED_EXTRACTION_KEYS = {
    "id",
    "event_time_start",
    "event_time_end",
    "time_precision",
    "location",
    "people",
    "direct_observations",
    "reported_information",
    "interpretations_or_concerns",
    "quotes",
    "caregiving_intervals",
    "substance_intervals",
    "sleep",
    "incidents_commitments_outcomes",
    "unknown_or_disputed",
    "corrections",
}
LIST_KEYS = {
    "people",
    "direct_observations",
    "reported_information",
    "interpretations_or_concerns",
    "quotes",
    "caregiving_intervals",
    "substance_intervals",
    "sleep",
    "incidents_commitments_outcomes",
    "unknown_or_disputed",
    "corrections",
}
TIME_PRECISIONS = {"exact", "approximate", "date_only", "unknown"}
SECTION_ITEM_KEYS = {
    "direct_observations": {
        "claim_id", "text", "basis", "source_person", "confidence",
        "disputed", "related_people",
    },
    "reported_information": {
        "claim_id", "text", "basis", "source_person", "confidence",
        "disputed", "related_people",
    },
    "interpretations_or_concerns": {
        "claim_id", "text", "basis", "source_person", "confidence",
        "disputed", "related_people",
    },
    "quotes": {"speaker", "text", "quote_type", "basis", "confidence"},
    "caregiving_intervals": {
        "caregiver", "child", "start", "end", "duration_minutes", "role",
        "activity", "basis", "confidence",
    },
    "substance_intervals": {
        "person", "substance", "start", "end", "quantity", "basis",
        "observable_basis", "functional_impact", "confidence",
    },
    "sleep": {"person", "start", "end", "duration_minutes", "interruptions", "basis"},
    "incidents_commitments_outcomes": {
        "claim_id", "text", "basis", "source_person", "confidence",
        "disputed", "related_people", "timestamp", "outcome",
    },
    "unknown_or_disputed": {
        "claim_id", "text", "basis", "source_person", "confidence",
        "disputed", "related_people",
    },
    "corrections": {
        "corrected_at", "author", "target", "original",
        "replacement_or_context", "reason",
    },
}


class ProcessorError(RuntimeError):
    """Non-sensitive processor failure."""


@dataclass(frozen=True)
class ProcessResult:
    status: str
    processed: int
    ids: list[str]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_dir(path: Path, mode: int = 0o700) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise ProcessorError("unsafe output directory")
        return
    if path.parent != path:
        _ensure_dir(path.parent, mode)
    path.mkdir(mode=mode)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    _fsync_dir(path.parent)


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path(".")
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ProcessorError("unsafe symlink path")


def _atomic_exclusive_or_verify(path: Path, data: bytes, *, mode: int = 0o600) -> str:
    """Publish bytes without overwriting; exact-content collisions are accepted."""
    _ensure_dir(path.parent)
    digest = _sha256(data)
    if path.is_symlink():
        raise ProcessorError("unsafe symlink path")
    if path.exists():
        existing = path.read_bytes()
        if _sha256(existing) == digest:
            return digest
        raise ProcessorError("output mismatch")

    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(tmp, flags, mode)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            existing = path.read_bytes()
            if _sha256(existing) != digest:
                raise ProcessorError("output mismatch")
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        _fsync_dir(path.parent)
        return digest
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _process_lock() -> Iterator[None]:
    lock_path = holding_dir() / "processor.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    try:
        os.chmod(lock_path, 0o600)
    except OSError:
        pass
    with lock_path.open("r+b") as lock_file:
        if msvcrt is not None and lock_path.stat().st_size == 0:
            lock_file.write(b" ")
            lock_file.flush()
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                lock_file.seek(0)
                getattr(msvcrt, "locking")(
                    lock_file.fileno(), getattr(msvcrt, "LK_NBLCK"), 1
                )
            else:  # pragma: no cover - every supported platform provides one
                raise ProcessorError("processor locking unavailable")
        except (BlockingIOError, OSError, PermissionError) as exc:
            raise ProcessorError("processor already running") from exc
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                lock_file.seek(0)
                getattr(msvcrt, "locking")(
                    lock_file.fileno(), getattr(msvcrt, "LK_UNLCK"), 1
                )


def _receipt_dir() -> Path:
    path = holding_dir() / "receipts"
    _ensure_dir(path)
    return path


def _manifest_dir() -> Path:
    path = holding_dir() / "batches"
    _ensure_dir(path)
    return path


def _completion_dir() -> Path:
    path = holding_dir() / "batch-completions"
    _ensure_dir(path)
    return path


def _receipt_path(entry_id: str) -> Path:
    return _receipt_dir() / f"{validate_entry_id(entry_id)}.json"


def _completion_path(manifest_id: str) -> Path:
    return _completion_dir() / f"{validate_entry_id(manifest_id)}.json"


def _parse_aware_timestamp(value: str) -> datetime:
    try:
        captured = datetime.fromisoformat(value)
    except Exception as exc:
        raise ProcessorError("invalid captured_at") from exc
    if captured.tzinfo is None or captured.utcoffset() is None:
        raise ProcessorError("captured_at must be timezone-aware")
    return captured


def _load_record(path: Path) -> dict[str, Any]:
    directory = holding_dir()
    if path.is_symlink() or path.name.startswith(".") or path.parent != directory:
        raise ProcessorError("unsafe holding record")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProcessorError("invalid holding record") from exc
    if data.get("schema_version") != 1:
        raise ProcessorError("unsupported holding record schema")
    entry_id = validate_entry_id(str(data.get("id") or ""))
    if path.name != f"{entry_id}.json":
        raise ProcessorError("holding record filename mismatch")
    if not isinstance(data.get("captured_at"), str) or not isinstance(data.get("text"), str):
        raise ProcessorError("invalid holding record shape")
    _parse_aware_timestamp(data["captured_at"])
    return data


def output_path(vault_path: str | Path, record: Mapping[str, Any]) -> Path:
    vault = Path(vault_path).expanduser()
    if vault.is_symlink():
        raise ProcessorError("unsafe symlink path")
    entry_id = validate_entry_id(str(record["id"]))
    captured = _parse_aware_timestamp(str(record["captured_at"]))
    day = captured.strftime("%Y-%m-%d")
    path = (
        vault
        / "entries"
        / captured.strftime("%Y")
        / captured.strftime("%m")
        / day
        / f"{captured.strftime('%H%M%S')}-{entry_id}.md"
    )
    _assert_no_symlink_components(path.parent)
    return path


def _load_receipt(entry_id: str) -> dict[str, Any] | None:
    path = _receipt_path(entry_id)
    if not path.exists():
        return None
    if path.is_symlink():
        raise ProcessorError("receipt mismatch")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProcessorError("receipt mismatch") from exc
    expected_keys = {
        "schema_version",
        "manifest_id",
        "id",
        "captured_at",
        "output_path",
        "output_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_keys:
        raise ProcessorError("receipt mismatch")
    return receipt


def _validate_receipt_contents(
    receipt: Mapping[str, Any], record: Mapping[str, Any], vault_path: str | Path
) -> bool:
    entry_id = validate_entry_id(str(record["id"]))
    if receipt.get("schema_version") != 1:
        raise ProcessorError("receipt mismatch")
    if (
        not isinstance(receipt.get("manifest_id"), str)
        or receipt.get("id") != entry_id
        or receipt.get("captured_at") != record.get("captured_at")
    ):
        raise ProcessorError("receipt mismatch")
    expected_path = output_path(vault_path, record)
    if receipt.get("output_path") != str(expected_path):
        raise ProcessorError("receipt mismatch")
    try:
        digest = _sha256(expected_path.read_bytes())
    except Exception as exc:
        raise ProcessorError("receipt mismatch") from exc
    if digest != receipt.get("output_sha256"):
        raise ProcessorError("receipt mismatch")
    return True


def _validate_receipt(record: Mapping[str, Any], vault_path: str | Path) -> bool:
    entry_id = validate_entry_id(str(record["id"]))
    receipt = _load_receipt(entry_id)
    if receipt is None:
        return False
    _validate_receipt_contents(receipt, record, vault_path)
    manifest_id = str(receipt["manifest_id"])
    marker = _load_completion_marker(manifest_id)
    if marker is None:
        return False
    if (
        marker.get("schema_version") != 1
        or marker.get("manifest_id") != manifest_id
        or entry_id not in marker.get("ids", [])
    ):
        raise ProcessorError("batch completion mismatch")
    outputs = marker.get("outputs")
    receipts = marker.get("receipts")
    captured_at = marker.get("captured_at")
    if (
        not isinstance(outputs, dict)
        or not isinstance(receipts, dict)
        or not isinstance(captured_at, dict)
        or entry_id not in outputs
        or entry_id not in receipts
        or captured_at.get(entry_id) != record.get("captured_at")
    ):
        raise ProcessorError("batch completion mismatch")
    if outputs[entry_id].get("output_sha256") != receipt.get("output_sha256"):
        raise ProcessorError("batch completion mismatch")
    if receipts[entry_id].get("receipt_sha256") != _sha256(_receipt_bytes(receipt)):
        raise ProcessorError("batch completion mismatch")
    return True


def _all_records() -> list[dict[str, Any]]:
    directory = holding_dir()
    return [_load_record(path) for path in sorted(directory.glob("*.json"))]


def _records_by_id(records: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(r["id"]): r for r in records}


def _build_prompt(records: Sequence[Mapping[str, Any]]) -> str:
    payload = [
        {
            "id": r["id"],
            "captured_at": r["captured_at"],
            "text": r["text"],
            "source": r.get("source") or {},
        }
        for r in records
    ]
    return (
        "Extract structured personal-history-log fields for each entry. "
        "Return only JSON with an `entries` array. Preserve uncertainty; never invent "
        "precision. Use basis labels for direct observations, reported information, "
        "inference/concern, exact/approximate/paraphrase quotes, unknowns, corrections, "
        "caregiving intervals, sleep, substances, work/incidents/commitments/timestamps. "
        "Each output entry must use the exact input id and must not include raw input text.\n\n"
        + json.dumps({"records": payload}, ensure_ascii=False, sort_keys=True)
    )


def _default_llm_call(**kwargs: Any) -> Any:
    from agent.auxiliary_client import call_llm

    return call_llm(**kwargs)


def extract_llm_content(response: Any) -> str:
    """Extract content from real auxiliary responses or string fakes."""
    if isinstance(response, str):
        content = response
    else:
        try:
            content = response.choices[0].message.content
        except Exception as exc:
            raise ProcessorError("malformed auxiliary response") from exc
    if not isinstance(content, str) or not content.strip():
        raise ProcessorError("empty auxiliary response")
    return content


def _validate_extraction(item: Mapping[str, Any]) -> dict[str, Any]:
    extra = set(item) - ALLOWED_EXTRACTION_KEYS
    if extra:
        raise ProcessorError("invalid structured extraction")
    entry_id = validate_entry_id(str(item.get("id") or ""))
    precision = item.get("time_precision", "unknown")
    if precision not in TIME_PRECISIONS:
        raise ProcessorError("invalid structured extraction")
    out: dict[str, Any] = {"id": entry_id}
    for key in ALLOWED_EXTRACTION_KEYS - {"id"}:
        value = item.get(key)
        if key in LIST_KEYS:
            if value is None:
                value = []
            if not isinstance(value, list):
                raise ProcessorError("invalid structured extraction")
            allowed_item_keys = SECTION_ITEM_KEYS.get(key)
            if allowed_item_keys is not None:
                for list_item in value:
                    if isinstance(list_item, dict):
                        if set(list_item) - allowed_item_keys:
                            raise ProcessorError("invalid structured extraction")
                    elif not isinstance(list_item, str):
                        raise ProcessorError("invalid structured extraction")
        elif key in {"event_time_start", "event_time_end", "location"}:
            if value is not None and not isinstance(value, str):
                raise ProcessorError("invalid structured extraction")
        elif key == "time_precision":
            value = precision
        out[key] = value
    return out


def _parse_model_response(raw: str, expected_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise ProcessorError("invalid model response") from exc
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ProcessorError("invalid model response")
    seen: dict[str, dict[str, Any]] = {}
    for item in entries:
        if not isinstance(item, dict):
            raise ProcessorError("invalid model response")
        valid = _validate_extraction(item)
        entry_id = valid["id"]
        if entry_id in seen:
            raise ProcessorError("duplicate model entry")
        seen[entry_id] = valid
    if set(seen) != set(expected_ids):
        raise ProcessorError("model response id mismatch")
    return seen


def _json_line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _list_section(values: Any) -> str:
    if not values:
        return "- \n"
    return "".join(f"- {_json_line(v)}\n" for v in values)


def extract_raw_section(markdown: str) -> str:
    rendered = markdown.encode("utf-8")
    match = RAW_SECTION_START_RE.search(rendered)
    if match is None:
        raise ProcessorError("raw section missing")
    raw_start = match.end()
    raw_length = int(match.group(1))
    raw = rendered[raw_start:raw_start + raw_length]
    raw_end = raw_start + raw_length
    expected_end = b"\n<!-- /private-journal-raw-v1 -->"
    if rendered[raw_end:raw_end + len(expected_end)] != expected_end:
        raise ProcessorError("raw section missing")
    if _sha256(raw) != match.group(2).decode("ascii"):
        raise ProcessorError("raw section mismatch")
    return raw.decode("utf-8")


def _render_markdown(record: Mapping[str, Any], extracted: Mapping[str, Any]) -> str:
    entry_id = validate_entry_id(str(record["id"]))
    raw = str(record["text"])
    raw_bytes = raw.encode("utf-8")
    raw_sha256 = _sha256(raw_bytes)
    front = {
        "entry_id": entry_id,
        "logged_at": record["captured_at"],
        "event_time_start": extracted.get("event_time_start"),
        "event_time_end": extracted.get("event_time_end"),
        "time_precision": extracted.get("time_precision") or "unknown",
        "location": extracted.get("location"),
        "people": extracted.get("people") or [],
        "input_mode": "text",
        "audio_file": None,
        "transcript_status": "not_applicable",
        "tags": ["personal-history-log"],
    }
    return (
        "---\n"
        + "\n".join(f"{k}: {_json_line(v)}" for k, v in front.items())
        + "\n---\n\n"
        "# Entry\n\n"
        "## Raw input — preserved unchanged\n\n"
        f"<!-- private-journal-raw-v1 bytes={len(raw_bytes)} sha256={raw_sha256} -->\n"
        f"{raw}"
        "\n<!-- /private-journal-raw-v1 -->\n\n"
        "## Structured extraction\n\n"
        "### Direct observations\n\n"
        + _list_section(extracted.get("direct_observations"))
        + "\n### Reported information\n\n"
        + _list_section(extracted.get("reported_information"))
        + "\n### Interpretations or concerns\n\n"
        + _list_section(extracted.get("interpretations_or_concerns"))
        + "\n### Quotes\n\n"
        + _list_section(extracted.get("quotes"))
        + "\n### Aria / caregiving intervals\n\n"
        + _list_section(extracted.get("caregiving_intervals"))
        + "\n### Substance intervals\n\n"
        + _list_section(extracted.get("substance_intervals"))
        + "\n### Sleep\n\n"
        + _list_section(extracted.get("sleep"))
        + "\n### Incidents, commitments, and outcomes\n\n"
        + _list_section(extracted.get("incidents_commitments_outcomes"))
        + "\n### Unknown or disputed\n\n"
        + _list_section(extracted.get("unknown_or_disputed"))
        + "\n## Corrections and later context — append only\n\n"
        + _list_section(extracted.get("corrections"))
    )


def _manifest_payload(records: Sequence[Mapping[str, Any]], extracted: Mapping[str, Any]) -> dict[str, Any]:
    ids = [str(r["id"]) for r in records]
    return {
        "schema_version": 1,
        "manifest_id": f"{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(6)}",
        "ids": ids,
        "captured_at": {str(r["id"]): r["captured_at"] for r in records},
        "extractions": {entry_id: extracted[entry_id] for entry_id in ids},
    }


def _write_manifest(records: Sequence[Mapping[str, Any]], extracted: Mapping[str, Any]) -> Path:
    payload = _manifest_payload(records, extracted)
    path = _manifest_dir() / f"{payload['manifest_id']}.json"
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    _atomic_exclusive_or_verify(path, data)
    return path


def _load_manifest(path: Path) -> dict[str, Any]:
    directory = _manifest_dir()
    if path.is_symlink() or path.parent != directory or path.name.startswith("."):
        raise ProcessorError("invalid batch manifest")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProcessorError("invalid batch manifest") from exc
    if data.get("schema_version") != 1:
        raise ProcessorError("invalid batch manifest")
    ids = data.get("ids")
    extractions = data.get("extractions")
    captured_at = data.get("captured_at")
    manifest_id = data.get("manifest_id")
    if (
        not isinstance(manifest_id, str)
        or path.name != f"{manifest_id}.json"
        or not isinstance(ids, list)
        or not isinstance(extractions, dict)
        or not isinstance(captured_at, dict)
        or not ids
        or len(ids) != len(set(ids))
        or set(extractions) != set(ids)
        or set(captured_at) != set(ids)
    ):
        raise ProcessorError("invalid batch manifest")
    for entry_id in ids:
        validate_entry_id(str(entry_id))
        if entry_id not in extractions or entry_id not in captured_at:
            raise ProcessorError("invalid batch manifest")
        _parse_aware_timestamp(str(captured_at[entry_id]))
        validated = _validate_extraction(extractions[entry_id])
        if validated["id"] != entry_id:
            raise ProcessorError("invalid batch manifest")
    return data


def _staged_ids() -> set[str]:
    ids: set[str] = set()
    for path in sorted(_manifest_dir().glob("*.json")):
        manifest = _load_manifest(path)
        ids.update(str(i) for i in manifest["ids"])
    return ids


def _receipt_payload(
    manifest_id: str, record: Mapping[str, Any], output: Path, output_sha256: str
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "manifest_id": manifest_id,
        "id": record["id"],
        "captured_at": record["captured_at"],
        "output_path": str(output),
        "output_sha256": output_sha256,
    }


def _receipt_bytes(receipt: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _write_receipt(
    manifest_id: str, record: Mapping[str, Any], output: Path, output_sha256: str
) -> str:
    receipt = _receipt_payload(manifest_id, record, output, output_sha256)
    data = _receipt_bytes(receipt)
    return _atomic_exclusive_or_verify(_receipt_path(str(record["id"])), data)


def _load_completion_marker(manifest_id: str) -> dict[str, Any] | None:
    path = _completion_path(manifest_id)
    if not path.exists():
        return None
    if path.is_symlink():
        raise ProcessorError("batch completion mismatch")
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProcessorError("batch completion mismatch") from exc
    expected_keys = {
        "schema_version",
        "manifest_id",
        "ids",
        "captured_at",
        "outputs",
        "receipts",
    }
    if not isinstance(marker, dict) or set(marker) != expected_keys:
        raise ProcessorError("batch completion mismatch")
    return marker


def _completion_payload(
    manifest: Mapping[str, Any],
    output_hashes: Mapping[str, str],
    receipt_hashes: Mapping[str, str],
) -> dict[str, Any]:
    ids = [str(i) for i in manifest["ids"]]
    return {
        "schema_version": 1,
        "manifest_id": manifest["manifest_id"],
        "ids": ids,
        "captured_at": {entry_id: manifest["captured_at"][entry_id] for entry_id in ids},
        "outputs": {
            entry_id: {"output_sha256": output_hashes[entry_id]} for entry_id in ids
        },
        "receipts": {
            entry_id: {"receipt_sha256": receipt_hashes[entry_id]} for entry_id in ids
        },
    }


def _write_completion_marker(
    manifest: Mapping[str, Any],
    output_hashes: Mapping[str, str],
    receipt_hashes: Mapping[str, str],
) -> None:
    payload = _completion_payload(manifest, output_hashes, receipt_hashes)
    data = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    _atomic_exclusive_or_verify(_completion_path(str(manifest["manifest_id"])), data)


def _validate_manifest_completion(
    manifest: Mapping[str, Any],
    records_by_id: Mapping[str, Mapping[str, Any]],
    vault: Path,
) -> bool:
    manifest_id = str(manifest["manifest_id"])
    marker = _load_completion_marker(manifest_id)
    if marker is None:
        return False
    ids = [str(i) for i in manifest["ids"]]
    if (
        marker.get("schema_version") != 1
        or marker.get("manifest_id") != manifest_id
        or marker.get("ids") != ids
        or marker.get("captured_at") != manifest["captured_at"]
    ):
        raise ProcessorError("batch completion mismatch")
    outputs = marker.get("outputs")
    receipts = marker.get("receipts")
    if not isinstance(outputs, dict) or not isinstance(receipts, dict):
        raise ProcessorError("batch completion mismatch")
    if set(outputs) != set(ids) or set(receipts) != set(ids):
        raise ProcessorError("batch completion mismatch")
    for entry_id in ids:
        record = records_by_id.get(entry_id)
        if record is None:
            raise ProcessorError("staged record missing")
        if record["captured_at"] != manifest["captured_at"][entry_id]:
            raise ProcessorError("staged record mismatch")
        receipt = _load_receipt(entry_id)
        if receipt is None:
            raise ProcessorError("batch completion mismatch")
        _validate_receipt_contents(receipt, record, vault)
        output_hash = _sha256(output_path(vault, record).read_bytes())
        if outputs[entry_id].get("output_sha256") != output_hash:
            raise ProcessorError("batch completion mismatch")
        if receipts[entry_id].get("receipt_sha256") != _sha256(_receipt_bytes(receipt)):
            raise ProcessorError("batch completion mismatch")
    return True


def _finish_manifest(path: Path, records_by_id: Mapping[str, Mapping[str, Any]], vault: Path) -> list[str]:
    manifest = _load_manifest(path)
    ids = [str(i) for i in manifest["ids"]]
    if _validate_manifest_completion(manifest, records_by_id, vault):
        return []
    records: dict[str, Mapping[str, Any]] = {}
    outputs: dict[str, Path] = {}
    output_hashes: dict[str, str] = {}
    receipt_hashes: dict[str, str] = {}
    manifest_id = str(manifest["manifest_id"])
    for entry_id in ids:
        record = records_by_id.get(entry_id)
        if record is None:
            raise ProcessorError("staged record missing")
        if record["captured_at"] != manifest["captured_at"][entry_id]:
            raise ProcessorError("staged record mismatch")
        records[entry_id] = record
        output = output_path(vault, record)
        rendered = _render_markdown(record, manifest["extractions"][entry_id]).encode("utf-8")
        output_hashes[entry_id] = _atomic_exclusive_or_verify(output, rendered)
        outputs[entry_id] = output

    for entry_id, output in outputs.items():
        output_hashes[entry_id] = _sha256(output.read_bytes())

    for entry_id in ids:
        record = records[entry_id]
        output = outputs[entry_id]
        receipt_hashes[entry_id] = _write_receipt(
            manifest_id, record, output, output_hashes[entry_id]
        )

    for entry_id in ids:
        receipt = _load_receipt(entry_id)
        if receipt is None:
            raise ProcessorError("receipt mismatch")
        _validate_receipt_contents(receipt, records[entry_id], vault)
        receipt_hashes[entry_id] = _sha256(_receipt_bytes(receipt))

    _write_completion_marker(manifest, output_hashes, receipt_hashes)
    if not _validate_manifest_completion(manifest, records_by_id, vault):
        raise ProcessorError("batch completion mismatch")
    return ids


def _resolve_vault_path(vault_path: str | Path | None) -> Path:
    if vault_path is None:
        cfg = load_config()
        vault_path = cfg_get(
            cfg,
            "plugins",
            "entries",
            "private-journal",
            "vault_path",
        )
    if not vault_path:
        raise ProcessorError("vault path is not configured")
    vault = Path(vault_path).expanduser()
    if vault.is_symlink():
        raise ProcessorError("unsafe symlink path")
    _assert_no_symlink_components(vault if vault.exists() else vault.parent)
    return vault


def _new_batch_records(records: Sequence[Mapping[str, Any]], vault: Path) -> list[Mapping[str, Any]]:
    staged = _staged_ids()
    pending: list[Mapping[str, Any]] = []
    for record in records:
        if _validate_receipt(record, vault):
            continue
        if str(record["id"]) in staged:
            continue
        pending.append(record)
    return pending


def process_pending(
    *,
    vault_path: str | Path | None = None,
    llm_call: Callable[..., Any] | None = None,
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
    max_batch_count: int = DEFAULT_MAX_BATCH_COUNT,
) -> ProcessResult:
    with _process_lock():
        vault = _resolve_vault_path(vault_path)
        records = _all_records()
        records_by_id = _records_by_id(records)

        processed: list[str] = []
        for manifest_path in sorted(_manifest_dir().glob("*.json")):
            processed.extend(_finish_manifest(manifest_path, records_by_id, vault))

        pending = _new_batch_records(records, vault)
        if not pending:
            status = "processed" if processed else "empty"
            return ProcessResult(status=status, processed=len(processed), ids=processed)
        if len(pending) > max_batch_count:
            raise ProcessorError("pending batch exceeds count bound")
        prompt = _build_prompt(pending)
        if len(prompt.encode("utf-8")) > max_batch_bytes:
            raise ProcessorError("pending batch exceeds byte bound")
        caller = llm_call or _default_llm_call
        try:
            response = caller(
                task=TASK_NAME,
                messages=[
                    {"role": "system", "content": "Return strict JSON only."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=DEFAULT_MAX_TOKENS,
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise ProcessorError("auxiliary model call failed") from exc
        content = extract_llm_content(response)
        extracted = _parse_model_response(content, [str(r["id"]) for r in pending])
        manifest_path = _write_manifest(pending, extracted)
        processed.extend(_finish_manifest(manifest_path, records_by_id, vault))
        return ProcessResult(status="processed", processed=len(processed), ids=processed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process private journal holding records.")
    parser.add_argument("--vault-path", default=None)
    parser.add_argument("--max-batch-bytes", type=int, default=DEFAULT_MAX_BATCH_BYTES)
    parser.add_argument("--max-batch-count", type=int, default=DEFAULT_MAX_BATCH_COUNT)
    args = parser.parse_args(argv)
    try:
        result = process_pending(
            vault_path=args.vault_path,
            max_batch_bytes=args.max_batch_bytes,
            max_batch_count=args.max_batch_count,
        )
    except (ProcessorError, PrivateJournalStoreError):
        print("private journal processor failed", file=sys.stderr)
        return 1
    if result.processed:
        print(json.dumps({"processed": result.processed, "ids": result.ids}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
