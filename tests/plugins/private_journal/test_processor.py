import json
import re
from types import SimpleNamespace
from pathlib import Path

import pytest

from plugins.private_journal import capture, processor


def _write_capture(home: Path, monkeypatch, text: str) -> dict:
    monkeypatch.setenv("HERMES_HOME", str(home))
    response = capture.capture_log(text)
    entry_id = response.split()[1]
    path = home / "journal" / "holding" / f"{entry_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_response(*ids: str) -> str:
    return json.dumps({
        "entries": [
            {
                "id": entry_id,
                "event_time_start": None,
                "event_time_end": None,
                "time_precision": "unknown",
                "location": None,
                "people": [],
                "direct_observations": [],
                "reported_information": [],
                "interpretations_or_concerns": [],
                "quotes": [],
                "caregiving_intervals": [],
                "substance_intervals": [],
                "sleep": [],
                "incidents_commitments_outcomes": [],
                "unknown_or_disputed": [],
                "corrections": [],
            }
            for entry_id in ids
        ]
    })


def _response_obj(content: str):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content)
            )
        ]
    )


def _receipt_exists(home: Path, entry_id: str) -> bool:
    return (home / "journal" / "holding" / "receipts" / f"{entry_id}.json").exists()


def _recover_raw_bytes(rendered: str) -> bytes:
    rendered_bytes = rendered.encode("utf-8")
    match = re.search(
        rb"<!-- private-journal-raw-v1 bytes=(\d+) sha256=([0-9a-f]{64}) -->\n",
        rendered_bytes,
    )
    assert match is not None
    start = match.end()
    length = int(match.group(1))
    raw = rendered_bytes[start:start + length]
    end_marker = b"\n<!-- /private-journal-raw-v1 -->"
    assert rendered_bytes[start + length:start + length + len(end_marker)] == end_marker
    assert processor._sha256(raw) == match.group(2).decode("ascii")
    return raw


def test_processor_empty_batch_no_llm_and_empty_stdout(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = processor.process_pending(vault_path=vault, llm_call=lambda **_: pytest.fail("no call"))

    assert result == processor.ProcessResult(status="empty", processed=0, ids=[])
    out = capsys.readouterr()
    assert out.out == ""


def test_process_lock_blocks_overlapping_processor(tmp_path, monkeypatch):
    home = tmp_path / "home-lock"
    vault = tmp_path / "vault"
    monkeypatch.setenv("HERMES_HOME", str(home))

    with processor._process_lock():
        with pytest.raises(processor.ProcessorError, match="already running"):
            processor.process_pending(vault_path=vault, llm_call=lambda **_: pytest.fail("no call"))


def test_process_lock_uses_windows_backend_when_fcntl_is_unavailable(tmp_path, monkeypatch):
    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self):
            self.calls = []

        def locking(self, fd, mode, count):
            self.calls.append((fd, mode, count))

    home = tmp_path / "home-lock-windows"
    monkeypatch.setenv("HERMES_HOME", str(home))
    fake = FakeMsvcrt()
    monkeypatch.setattr(processor, "fcntl", None)
    monkeypatch.setattr(processor, "msvcrt", fake)

    with processor._process_lock():
        pass

    assert [call[1] for call in fake.calls] == [fake.LK_NBLCK, fake.LK_UNLCK]


def test_processor_batches_all_pending_in_one_call_and_writes_receipts(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    first = _write_capture(home, monkeypatch, "raw one — unchanged  ")
    second = _write_capture(home, monkeypatch, "raw two\nline  ")
    calls = []

    def fake_llm(**kwargs):
        calls.append(kwargs)
        return _extract_response(first["id"], second["id"])

    result = processor.process_pending(vault_path=vault, llm_call=fake_llm)

    assert result.processed == 2
    assert set(result.ids) == {first["id"], second["id"]}
    assert len(calls) == 1
    assert calls[0]["task"] == "private_journal_batch"
    outputs = sorted((vault / "entries").glob("*/*/*/*.md"))
    assert len(outputs) == 2
    rendered = "\n".join(p.read_text(encoding="utf-8") for p in outputs)
    assert "raw one — unchanged  " in rendered
    assert "raw two\nline  " in rendered
    receipts = sorted((home / "journal" / "holding" / "receipts").glob("*.json"))
    assert {p.stem for p in receipts} == {first["id"], second["id"]}
    assert len(list((home / "journal" / "holding").glob("*.json"))) == 2


def test_processor_over_bound_fails_without_llm_or_receipt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    record = _write_capture(home, monkeypatch, "x" * 20)

    with pytest.raises(processor.ProcessorError):
        processor.process_pending(
            vault_path=vault,
            llm_call=lambda **_: pytest.fail("no call"),
            max_batch_bytes=10,
        )

    assert not (home / "journal" / "holding" / "receipts" / f"{record['id']}.json").exists()


def test_processor_invalid_llm_output_retries_without_receipt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    record = _write_capture(home, monkeypatch, "retry me")

    with pytest.raises(processor.ProcessorError):
        processor.process_pending(vault_path=vault, llm_call=lambda **_: '{"entries":[]}')

    assert not (home / "journal" / "holding" / "receipts" / f"{record['id']}.json").exists()


def test_processor_idempotent_existing_output_and_receipt_no_duplicate(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    record = _write_capture(home, monkeypatch, "same")

    processor.process_pending(vault_path=vault, llm_call=lambda **_: _extract_response(record["id"]))
    outputs_before = sorted((vault / "entries").glob("*/*/*/*.md"))

    result = processor.process_pending(vault_path=vault, llm_call=lambda **_: pytest.fail("no call"))

    assert result == processor.ProcessResult(status="empty", processed=0, ids=[])
    assert sorted((vault / "entries").glob("*/*/*/*.md")) == outputs_before


def test_extract_llm_content_accepts_openai_response_object_and_string():
    payload = _extract_response("20260819T000000-abcdef123456")

    assert processor.extract_llm_content(_response_obj(payload)) == payload
    assert processor.extract_llm_content(payload) == payload


@pytest.mark.parametrize("bad", ["", SimpleNamespace(), SimpleNamespace(choices=[])])
def test_extract_llm_content_rejects_empty_or_malformed_without_payload(bad):
    with pytest.raises(processor.ProcessorError, match="empty|malformed"):
        processor.extract_llm_content(bad)


def test_processor_passes_explicit_auxiliary_bounds(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    record = _write_capture(home, monkeypatch, "bounded")
    calls = []

    def fake_llm(**kwargs):
        calls.append(kwargs)
        return _response_obj(_extract_response(record["id"]))

    processor.process_pending(vault_path=vault, llm_call=fake_llm)

    assert calls[0]["task"] == "private_journal_batch"
    assert calls[0]["max_tokens"] == processor.DEFAULT_MAX_TOKENS
    assert calls[0]["timeout"] == processor.DEFAULT_TIMEOUT_SECONDS


def test_corrupt_receipt_fails_closed_instead_of_skipping_record(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    record = _write_capture(home, monkeypatch, "receipt mismatch")
    receipt_dir = home / "journal" / "holding" / "receipts"
    receipt_dir.mkdir(mode=0o700)
    (receipt_dir / f"{record['id']}.json").write_text('{"schema_version":1}', encoding="utf-8")

    with pytest.raises(processor.ProcessorError, match="receipt"):
        processor.process_pending(vault_path=vault, llm_call=lambda **_: pytest.fail("no call"))


def test_staged_manifest_recovers_without_second_model_call_after_output_before_receipt(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    record = _write_capture(home, monkeypatch, "recover once")

    def crash_after_output(*args, **kwargs):
        raise RuntimeError("simulated crash")

    with monkeypatch.context() as m:
        m.setattr(processor, "_write_receipt", crash_after_output)
        with pytest.raises(RuntimeError):
            processor.process_pending(
                vault_path=vault,
                llm_call=lambda **_: _extract_response(record["id"]),
            )
    outputs = sorted((vault / "entries").glob("*/*/*/*.md"))
    assert len(outputs) == 1

    calls = []
    result = processor.process_pending(
        vault_path=vault,
        llm_call=lambda **kwargs: calls.append(kwargs) or pytest.fail("no second call"),
    )

    assert result.processed == 1
    assert calls == []
    assert sorted((vault / "entries").glob("*/*/*/*.md")) == outputs
    assert (home / "journal" / "holding" / "receipts" / f"{record['id']}.json").exists()


def test_recovery_also_batches_new_pending_records_in_same_run(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    staged = _write_capture(home, monkeypatch, "staged")

    def crash_receipt(*_args, **_kwargs):
        raise RuntimeError("crash")

    with monkeypatch.context() as m:
        m.setattr(processor, "_write_receipt", crash_receipt)
        with pytest.raises(RuntimeError):
            processor.process_pending(
                vault_path=vault,
                llm_call=lambda **_: _extract_response(staged["id"]),
            )

    fresh = _write_capture(home, monkeypatch, "fresh")
    calls = []
    result = processor.process_pending(
        vault_path=vault,
        llm_call=lambda **kwargs: calls.append(kwargs) or _extract_response(fresh["id"]),
    )

    assert result.processed == 2
    assert set(result.ids) == {staged["id"], fresh["id"]}
    assert len(calls) == 1


def test_manifest_rejects_extraction_key_id_mismatch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    record = _write_capture(home, monkeypatch, "manifest")
    extracted = processor._parse_model_response(
        _extract_response(record["id"]), [record["id"]]
    )
    path = processor._write_manifest([record], extracted)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["extractions"][record["id"]]["id"] = "20260819T000000-abcdef123456"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(processor.ProcessorError, match="manifest"):
        processor.process_pending(
            vault_path=vault, llm_call=lambda **_: pytest.fail("no call")
        )


def test_existing_output_mismatch_fails_closed_before_receipt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    record = _write_capture(home, monkeypatch, "immutable")
    output = processor.output_path(vault, record)
    output.parent.mkdir(parents=True)
    output.write_text("different", encoding="utf-8")

    with pytest.raises(processor.ProcessorError, match="output mismatch"):
        processor.process_pending(vault_path=vault, llm_call=lambda **_: _extract_response(record["id"]))

    assert not (home / "journal" / "holding" / "receipts" / f"{record['id']}.json").exists()


def test_output_write_failure_preserves_staged_retry_without_second_model_call(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    record = _write_capture(home, monkeypatch, "write retry")
    real_publish = processor._atomic_exclusive_or_verify
    failed = {"done": False}

    def fail_output_once(path, data, *, mode=0o600):
        if path.suffix == ".md" and not failed["done"]:
            failed["done"] = True
            raise OSError("simulated output failure")
        return real_publish(path, data, mode=mode)

    with monkeypatch.context() as m:
        m.setattr(processor, "_atomic_exclusive_or_verify", fail_output_once)
        with pytest.raises(OSError):
            processor.process_pending(
                vault_path=vault,
                llm_call=lambda **_: _extract_response(record["id"]),
            )

    calls = []
    result = processor.process_pending(
        vault_path=vault,
        llm_call=lambda **kwargs: calls.append(kwargs) or pytest.fail("no second call"),
    )

    assert result.processed == 1
    assert calls == []
    assert (home / "journal" / "holding" / "receipts" / f"{record['id']}.json").exists()


def test_partial_receipt_from_incomplete_batch_is_not_processed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    first = _write_capture(home, monkeypatch, "first incomplete")
    second = _write_capture(home, monkeypatch, "second incomplete")
    receipt_writes = {"count": 0}
    real_write_receipt = processor._write_receipt

    def crash_after_first_receipt(*args, **kwargs):
        receipt_writes["count"] += 1
        real_write_receipt(*args, **kwargs)
        if receipt_writes["count"] == 1:
            raise RuntimeError("simulated crash after first receipt")

    with monkeypatch.context() as m:
        m.setattr(processor, "_write_receipt", crash_after_first_receipt)
        with pytest.raises(RuntimeError):
            processor.process_pending(
                vault_path=vault,
                llm_call=lambda **_: _extract_response(first["id"], second["id"]),
            )

    assert len(list((vault / "entries").glob("*/*/*/*.md"))) == 2
    receipts = sorted((home / "journal" / "holding" / "receipts").glob("*.json"))
    assert len(receipts) == 1
    partial_record = {
        first["id"]: first,
        second["id"]: second,
    }[receipts[0].stem]
    assert not processor._validate_receipt(partial_record, vault)

    calls = []
    result = processor.process_pending(
        vault_path=vault,
        llm_call=lambda **kwargs: calls.append(kwargs) or pytest.fail("no second call"),
    )

    assert result.processed == 2
    assert set(result.ids) == {first["id"], second["id"]}
    assert calls == []
    assert processor._validate_receipt(first, vault)
    assert processor._validate_receipt(second, vault)


def test_batch_output_failure_does_not_recognize_subset_and_retries_without_model_call(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    first = _write_capture(home, monkeypatch, "first output")
    second = _write_capture(home, monkeypatch, "second output")
    real_publish = processor._atomic_exclusive_or_verify
    output_writes = {"count": 0}

    def fail_second_output(path, data, *, mode=0o600):
        if path.suffix == ".md":
            output_writes["count"] += 1
            if output_writes["count"] == 2:
                raise OSError("simulated second output failure")
        return real_publish(path, data, mode=mode)

    with monkeypatch.context() as m:
        m.setattr(processor, "_atomic_exclusive_or_verify", fail_second_output)
        with pytest.raises(OSError):
            processor.process_pending(
                vault_path=vault,
                llm_call=lambda **_: _extract_response(first["id"], second["id"]),
            )

    manifest_order = sorted([first, second], key=lambda record: record["id"])
    assert sorted(p.name for p in (vault / "entries").glob("*/*/*/*.md")) == [
        processor.output_path(vault, manifest_order[0]).name
    ]
    assert not _receipt_exists(home, first["id"])
    assert not _receipt_exists(home, second["id"])

    calls = []
    result = processor.process_pending(
        vault_path=vault,
        llm_call=lambda **kwargs: calls.append(kwargs) or pytest.fail("no second call"),
    )

    assert result.processed == 2
    assert set(result.ids) == {first["id"], second["id"]}
    assert calls == []
    assert len(list((vault / "entries").glob("*/*/*/*.md"))) == 2


def test_invalid_model_shape_extra_key_retries_without_receipt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    record = _write_capture(home, monkeypatch, "bad shape")
    bad = json.loads(_extract_response(record["id"]))
    bad["entries"][0]["raw_text"] = "model must not echo raw"

    with pytest.raises(processor.ProcessorError, match="structured"):
        processor.process_pending(vault_path=vault, llm_call=lambda **_: json.dumps(bad))

    assert not (home / "journal" / "holding" / "receipts" / f"{record['id']}.json").exists()


def test_invalid_nested_structured_shape_retries_without_receipt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    record = _write_capture(home, monkeypatch, "bad nested shape")
    bad = json.loads(_extract_response(record["id"]))
    bad["entries"][0]["quotes"] = [{"speaker": "A", "raw_payload": "not allowed"}]

    with pytest.raises(processor.ProcessorError, match="structured"):
        processor.process_pending(vault_path=vault, llm_call=lambda **_: json.dumps(bad))

    assert not (home / "journal" / "holding" / "receipts" / f"{record['id']}.json").exists()


def test_raw_triple_backticks_use_dynamic_fence_and_remain_unchanged(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    raw = "before ``` inside\n```` longer\nend"
    record = _write_capture(home, monkeypatch, raw)

    processor.process_pending(vault_path=vault, llm_call=lambda **_: _extract_response(record["id"]))

    output = next((vault / "entries").glob("*/*/*/*.md"))
    rendered = output.read_text(encoding="utf-8")
    assert _recover_raw_bytes(rendered) == raw.encode("utf-8")
    assert "```text" not in rendered


@pytest.mark.parametrize(
    "raw",
    [
        "no trailing newline",
        "trailing spaces  \nand tabs\t\t\n\n",
        "Unicode: café 雪 👩‍👧\n",
        "```python\n# not a fence boundary for storage\n```\n# Heading-like raw\n````\n`",
    ],
)
def test_raw_section_extracts_original_utf8_bytes_from_rendered_markdown(raw):
    record = {
        "id": "20260819T000000-abcdef123456",
        "captured_at": "2026-08-19T00:00:00+00:00",
        "text": raw,
    }
    extracted = processor._parse_model_response(
        _extract_response(record["id"]), [record["id"]]
    )[record["id"]]

    rendered = processor._render_markdown(record, extracted)

    assert rendered.index("# Entry") < rendered.index("## Raw input — preserved unchanged")
    assert rendered.index("## Raw input — preserved unchanged") < rendered.index("## Structured extraction")
    assert _recover_raw_bytes(rendered) == raw.encode("utf-8")
    assert "```text" not in rendered


def test_naive_captured_at_is_rejected(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    record = _write_capture(home, monkeypatch, "naive time")
    path = home / "journal" / "holding" / f"{record['id']}.json"
    record["captured_at"] = "2026-08-19T00:00:00"
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(processor.ProcessorError, match="timezone"):
        processor.process_pending(vault_path=vault, llm_call=lambda **_: _extract_response(record["id"]))


def test_vault_symlink_rejected_before_output(tmp_path, monkeypatch):
    home = tmp_path / "home"
    real_vault = tmp_path / "real-vault"
    link_vault = tmp_path / "vault-link"
    real_vault.mkdir()
    link_vault.symlink_to(real_vault, target_is_directory=True)
    record = _write_capture(home, monkeypatch, "symlink vault")

    with pytest.raises(processor.ProcessorError, match="symlink|unsafe"):
        processor.process_pending(vault_path=link_vault, llm_call=lambda **_: _extract_response(record["id"]))


def test_configured_vault_path_is_read_from_profile_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    record = _write_capture(home, monkeypatch, "from config")
    (home / "config.yaml").write_text(
        "plugins:\n  entries:\n    private-journal:\n      vault_path: "
        f"{vault}\n",
        encoding="utf-8",
    )

    processor.process_pending(vault_path=None, llm_call=lambda **_: _extract_response(record["id"]))

    assert next((vault / "entries").glob("*/*/*/*.md")).name.endswith(f"-{record['id']}.md")
