from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_plugins.cron_event_subscriber import subscriber as subscriber_module
from hermes_plugins.cron_event_subscriber.subscriber import EventSubscriber


def _event(event_id: str, profile: str = "writer") -> dict:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": "create",
        "emitted_at": "2026-07-21T12:00:00Z",
        "source_profile": profile,
        "job_id": f"job-{event_id}",
        "job": {"id": f"job-{event_id}", "name": "safe"},
    }


def _pending(root: Path, event: dict, *, filename: str | None = None) -> Path:
    directory = root / "pending" / event["source_profile"]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (filename or f"{event['event_id']}.json")
    path.write_text(json.dumps(event), encoding="utf-8")
    return path


def test_callback_failure_leaves_event_retryable_and_ack_happens_after_success(tmp_path):
    path = _pending(tmp_path, _event("retry-me"))
    subscriber = EventSubscriber(tmp_path)

    def fail(_event):
        raise RuntimeError("temporary downstream failure")

    first = subscriber.drain(fail)
    assert first.failed == 1
    assert path.exists()
    assert not list((tmp_path / "acknowledged").rglob("*.json"))

    seen = []
    second = subscriber.drain(seen.append)
    assert second.processed == 1
    assert [event["event_id"] for event in seen] == ["retry-me"]
    assert not path.exists()
    assert (tmp_path / "acknowledged" / "retry-me.json").exists()


def test_reentrant_maintenance_fails_fast_instead_of_deadlocking(tmp_path):
    _pending(tmp_path, _event("reentrant"))
    subscriber = EventSubscriber(tmp_path)

    def callback(_event):
        with pytest.raises(RuntimeError, match="reentrant"):
            subscriber.maintain()

    result = subscriber.drain(callback)

    assert result.processed == 1


def test_duplicate_event_id_is_suppressed(tmp_path):
    subscriber = EventSubscriber(tmp_path)
    first = _event("same-id", "writer")
    _pending(tmp_path, first)
    seen = []
    assert subscriber.drain(seen.append).processed == 1

    duplicate = _event("same-id", "ops")
    duplicate["job_id"] = "different-job"
    duplicate_path = _pending(tmp_path, duplicate)
    result = subscriber.drain(seen.append)

    assert result.duplicates == 1
    assert len(seen) == 1
    assert not duplicate_path.exists()


def test_concurrent_duplicate_event_id_invokes_callback_once(tmp_path):
    _pending(tmp_path, _event("same-id", "writer"), filename="writer.json")
    _pending(tmp_path, _event("same-id", "ops"), filename="ops.json")
    callback_started = threading.Event()
    release_callback = threading.Event()
    seen = []

    callback_lock = threading.Lock()

    def slow_callback(event):
        with callback_lock:
            seen.append(event["source_profile"])
            is_first = len(seen) == 1
        if is_first:
            callback_started.set()
            assert release_callback.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(EventSubscriber(tmp_path).drain, slow_callback)
        assert callback_started.wait(timeout=5)
        second_started = threading.Event()

        def run_second():
            second_started.set()
            return EventSubscriber(tmp_path).drain(slow_callback)

        second = pool.submit(run_second)
        assert second_started.wait(timeout=5)
        assert not second.done()
        release_callback.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert len(seen) == 1
    assert first_result.processed + second_result.processed == 1
    assert first_result.duplicates + second_result.duplicates >= 1
    assert (tmp_path / "acknowledged" / "same-id.json").exists()
    assert not list((tmp_path / "pending").glob("*/*.json"))


@pytest.mark.skipif(os.name == "nt", reason="requires the POSIX fork context")
def test_cross_process_duplicate_event_id_invokes_callback_once(tmp_path):
    _pending(tmp_path, _event("process-id", "writer"), filename="writer.json")
    _pending(tmp_path, _event("process-id", "ops"), filename="ops.json")
    context = multiprocessing.get_context("fork")
    callback_started = context.Event()
    release_callback = context.Event()
    callback_log = tmp_path / "callbacks.log"

    def run_consumer(block: bool) -> None:
        def callback(event):
            with callback_log.open("a", encoding="utf-8") as handle:
                handle.write(f"{event['source_profile']}\n")
                handle.flush()
                os.fsync(handle.fileno())
            callback_started.set()
            if block:
                assert release_callback.wait(timeout=5)

        EventSubscriber(tmp_path).drain(callback)

    first = context.Process(target=run_consumer, args=(True,))
    first.start()
    assert callback_started.wait(timeout=5)

    second = context.Process(target=run_consumer, args=(False,))
    second.start()
    time.sleep(0.2)
    assert second.is_alive()
    assert callback_log.read_text(encoding="utf-8").splitlines() == ["ops"]

    release_callback.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert callback_log.read_text(encoding="utf-8").splitlines() == ["ops"]
    assert (tmp_path / "acknowledged" / "process-id.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="requires the POSIX fork context")
def test_process_crash_releases_lock_and_recovers_processing_event(tmp_path):
    _pending(tmp_path, _event("crash-recovery"))
    context = multiprocessing.get_context("fork")
    callback_started = context.Event()

    def run_crashing_consumer() -> None:
        def callback(_event):
            callback_started.set()
            time.sleep(30)

        EventSubscriber(tmp_path, claim_timeout_seconds=0).drain(callback)

    child = context.Process(target=run_crashing_consumer)
    child.start()
    assert callback_started.wait(timeout=5)
    child.terminate()
    child.join(timeout=5)

    assert child.exitcode is not None
    assert list((tmp_path / "processing" / "writer").glob("*.json"))

    seen = []
    result = EventSubscriber(tmp_path, claim_timeout_seconds=0).drain(seen.append)

    assert result.recovered == 1
    assert result.processed == 1
    assert [event["event_id"] for event in seen] == ["crash-recovery"]
    assert (tmp_path / "acknowledged" / "crash-recovery.json").exists()


def test_windows_lock_fallback_acquires_and_releases(tmp_path, monkeypatch):
    calls = []

    def fake_locking(_descriptor, mode, count):
        calls.append((mode, count))

    monkeypatch.setattr(subscriber_module, "fcntl", None)
    monkeypatch.setattr(subscriber_module, "_msvcrt_locking", fake_locking)
    monkeypatch.setattr(subscriber_module, "_msvcrt_lock_nonblocking", 1)
    monkeypatch.setattr(subscriber_module, "_msvcrt_unlock", 2)

    EventSubscriber(tmp_path).maintain()

    assert calls == [(1, 1), (2, 1)]


def test_subscriber_fsyncs_each_new_directory_boundary(tmp_path, monkeypatch):
    root = tmp_path / "events" / "cron"
    fsynced = []
    monkeypatch.setattr(subscriber_module, "_SECURE_DIR_FD", False)
    monkeypatch.setattr(subscriber_module, "_fsync_directory", fsynced.append)

    subscriber_module._ensure_directory(root / "pending" / "writer")

    assert tmp_path in fsynced
    assert tmp_path / "events" in fsynced
    assert root in fsynced
    assert root / "pending" in fsynced


def test_subscriber_rejects_symlinked_profile_without_touching_external_file(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    external_event = external / "event.json"
    external_event.write_text(json.dumps(_event("external")), encoding="utf-8")
    profile_dir = tmp_path / "pending" / "writer"
    profile_dir.parent.mkdir(parents=True)
    profile_dir.symlink_to(external, target_is_directory=True)

    result = EventSubscriber(tmp_path).drain(lambda _event: pytest.fail("must not dispatch"))

    assert result.processed == 0
    assert external_event.exists()
    assert not (tmp_path / "acknowledged" / "external.json").exists()
    assert not list((tmp_path / "processing").rglob("*")) if (tmp_path / "processing").exists() else True


def test_subscriber_dirfd_blocks_source_directory_ordinary_swap(tmp_path, monkeypatch):
    if not subscriber_module._SECURE_DIR_FD:
        pytest.skip("requires POSIX directory-fd support")
    pending = _pending(tmp_path, _event("source-race"), filename="race.json")
    displaced = tmp_path / "displaced-writer"
    external = tmp_path / "external"
    external.mkdir()
    external_event = external / pending.name
    external_event.write_text("external-sentinel", encoding="utf-8")
    original_drain_pending = EventSubscriber._drain_pending
    swapped = False
    delivered = []

    def swap_after_profile_pin(self, path, profile, callback, result):
        nonlocal swapped
        if not swapped:
            path.parent.rename(displaced)
            external.rename(path.parent)
            swapped = True
        return original_drain_pending(self, path, profile, callback, result)

    monkeypatch.setattr(EventSubscriber, "_drain_pending", swap_after_profile_pin)

    result = EventSubscriber(tmp_path).drain(delivered.append)

    assert result.processed == 1
    assert [event["event_id"] for event in delivered] == ["source-race"]
    replacement_event = tmp_path / "pending" / "writer" / pending.name
    assert replacement_event.read_text(encoding="utf-8") == "external-sentinel"
    assert not (tmp_path / "acknowledged" / "external.json").exists()
    assert (tmp_path / "acknowledged" / "source-race.json").exists()


def test_maintenance_pins_processing_and_retention_directories(tmp_path, monkeypatch):
    if not subscriber_module._SECURE_DIR_FD:
        pytest.skip("requires POSIX directory-fd support")

    processing = tmp_path / "processing" / "writer"
    processing.mkdir(parents=True)
    processing_file = processing / "recover.json"
    processing_file.write_text(json.dumps(_event("recover")), encoding="utf-8")
    old = time.time() - 600
    os.utime(processing_file, (old, old))

    external_processing = tmp_path / "external-processing"
    external_processing.mkdir()
    external_processing_file = external_processing / "recover.json"
    external_processing_file.write_text("EXTERNAL-PROCESSING-SENTINEL", encoding="utf-8")
    os.utime(external_processing_file, (old, old))
    displaced_processing = tmp_path / "displaced-processing"

    original_replace = subscriber_module._durable_replace
    swapped = {"done": False}

    def swap_processing(source, destination):
        if not swapped["done"] and source == processing_file:
            processing.rename(displaced_processing)
            external_processing.rename(processing)
            swapped["done"] = True
        return original_replace(source, destination)

    monkeypatch.setattr(subscriber_module, "_durable_replace", swap_processing)

    recovered = EventSubscriber(tmp_path, claim_timeout_seconds=0).maintain()
    assert recovered.recovered == 1
    # Replacement path is left alone; original inode is recovered under pending.
    assert processing.joinpath("recover.json").read_text(encoding="utf-8") == (
        "EXTERNAL-PROCESSING-SENTINEL"
    )
    assert (tmp_path / "pending" / "writer" / "recover.json").exists()
    assert not displaced_processing.joinpath("recover.json").exists()

    ack = tmp_path / "acknowledged"
    ack.mkdir(parents=True)
    original_ack = ack / "old.json"
    original_ack.write_text("ORIGINAL-ACK", encoding="utf-8")
    older = time.time() - 10 * 86400
    os.utime(original_ack, (older, older))
    external_ack = tmp_path / "external-ack"
    external_ack.mkdir()
    external_ack_file = external_ack / "old.json"
    external_ack_file.write_text("EXTERNAL-ACK-SENTINEL", encoding="utf-8")
    os.utime(external_ack_file, (older, older))
    displaced_ack = tmp_path / "displaced-ack"

    original_list = subscriber_module._list_child_names
    swapped_ack = {"done": False}

    def list_then_swap(parent):
        names = original_list(parent)
        if parent == ack and not swapped_ack["done"]:
            ack.rename(displaced_ack)
            external_ack.rename(ack)
            swapped_ack["done"] = True
        return names

    monkeypatch.setattr(subscriber_module, "_list_child_names", list_then_swap)
    cleaned = EventSubscriber(tmp_path, retention_days=1).maintain()
    assert cleaned.cleaned >= 1
    assert (ack / "old.json").read_text(encoding="utf-8") == "EXTERNAL-ACK-SENTINEL"
    assert not (displaced_ack / "old.json").exists()


def test_successful_transition_fsyncs_subscriber_state(tmp_path, monkeypatch):
    _pending(tmp_path, _event("durable"))
    calls = []
    monkeypatch.setattr(subscriber_module.os, "fsync", calls.append)

    result = EventSubscriber(tmp_path).drain(lambda _event: None)

    assert result.processed == 1
    assert len(calls) >= 2


def test_drain_limit_bounds_each_consumer_batch(tmp_path):
    _pending(tmp_path, _event("first"), filename="001.json")
    _pending(tmp_path, _event("second"), filename="002.json")
    seen = []

    result = EventSubscriber(tmp_path).drain(seen.append, limit=1)

    assert result.processed == 1
    assert len(seen) == 1
    assert len(list((tmp_path / "pending").glob("*/*.json"))) == 1


def test_stale_claim_is_recovered_after_crash(tmp_path):
    processing = tmp_path / "processing" / "writer"
    processing.mkdir(parents=True)
    claimed = processing / "recover.json"
    claimed.write_text(json.dumps(_event("recover")), encoding="utf-8")
    old = time.time() - 600
    os.utime(claimed, (old, old))

    seen = []
    result = EventSubscriber(tmp_path, claim_timeout_seconds=60).drain(seen.append)

    assert result.recovered == 1
    assert result.processed == 1
    assert [event["event_id"] for event in seen] == ["recover"]
    assert not claimed.exists()


def test_old_pending_event_is_not_recovered_while_callback_is_active(tmp_path):
    pending = _pending(tmp_path, _event("old-active"))
    old = time.time() - 600
    os.utime(pending, (old, old))
    callback_started = threading.Event()
    release_callback = threading.Event()

    def slow_callback(_event):
        callback_started.set()
        assert release_callback.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            EventSubscriber(tmp_path, claim_timeout_seconds=60).drain,
            slow_callback,
        )
        assert callback_started.wait(timeout=5)
        second_started = threading.Event()

        def run_second():
            second_started.set()
            return EventSubscriber(tmp_path, claim_timeout_seconds=60).drain(
                lambda _event: pytest.fail("active callback must not be replayed")
            )

        second_future = pool.submit(run_second)
        assert second_started.wait(timeout=5)
        assert not second_future.done()
        release_callback.set()
        first_result = first.result(timeout=5)
        second = second_future.result(timeout=5)

    assert second.recovered == 0
    assert first_result.processed == 1
    assert (tmp_path / "acknowledged" / "old-active.json").exists()


def test_expired_timeout_does_not_reclaim_an_active_callback(tmp_path):
    _pending(tmp_path, _event("lease-active"))
    callback_started = threading.Event()
    release_callback = threading.Event()
    seen = []

    def slow_callback(event):
        seen.append(event["event_id"])
        callback_started.set()
        assert release_callback.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            EventSubscriber(tmp_path, claim_timeout_seconds=0).drain,
            slow_callback,
        )
        assert callback_started.wait(timeout=5)
        second_started = threading.Event()

        def run_second():
            second_started.set()
            return EventSubscriber(tmp_path, claim_timeout_seconds=0).drain(
                lambda _event: pytest.fail("active callback must retain its lease")
            )

        second_future = pool.submit(run_second)
        assert second_started.wait(timeout=5)
        assert not second_future.done()
        release_callback.set()
        first_result = first.result(timeout=5)
        second = second_future.result(timeout=5)

    assert seen == ["lease-active"]
    assert second.recovered == 0
    assert first_result.processed == 1
    assert (tmp_path / "acknowledged" / "lease-active.json").exists()


def test_acknowledgement_retention_starts_at_ack_time(tmp_path):
    pending = _pending(tmp_path, _event("old-event", "writer"))
    old = time.time() - 40 * 86400
    os.utime(pending, (old, old))
    seen = []
    subscriber = EventSubscriber(tmp_path, retention_days=30)

    assert subscriber.drain(seen.append).processed == 1
    acknowledgement = tmp_path / "acknowledged" / "old-event.json"
    assert time.time() - acknowledgement.stat().st_mtime < 60

    _pending(tmp_path, _event("old-event", "ops"))
    duplicate = subscriber.drain(seen.append)

    assert duplicate.processed == 0
    assert duplicate.duplicates == 1
    assert len(seen) == 1


def test_retention_removes_old_acknowledgements_and_quarantine(tmp_path):
    subscriber = EventSubscriber(tmp_path, retention_days=7)
    ack = tmp_path / "acknowledged" / "old.json"
    quarantine = tmp_path / "quarantine" / "writer" / "bad.json"
    ack.parent.mkdir(parents=True)
    quarantine.parent.mkdir(parents=True)
    ack.write_text(json.dumps(_event("old")), encoding="utf-8")
    quarantine.write_text("bad", encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
    os.utime(ack, (old, old))
    os.utime(quarantine, (old, old))

    result = subscriber.maintain()

    assert result.cleaned == 2
    assert not ack.exists()
    assert not quarantine.exists()


def test_malformed_is_quarantined_and_temporary_files_are_ignored_then_cleaned(tmp_path):
    pending = tmp_path / "pending" / "writer"
    pending.mkdir(parents=True)
    malformed = pending / "bad.json"
    malformed.write_text('{"event_id":', encoding="utf-8")
    recent_tmp = pending / ".publish-recent.tmp"
    stale_tmp = pending / ".publish-stale.tmp"
    recent_tmp.write_text("partial", encoding="utf-8")
    stale_tmp.write_text("partial", encoding="utf-8")
    old = time.time() - 600
    os.utime(stale_tmp, (old, old))

    result = EventSubscriber(tmp_path, temporary_retention_seconds=60).drain(lambda _event: None)

    assert result.malformed == 1
    assert result.cleaned == 1
    assert not malformed.exists()
    assert (tmp_path / "quarantine" / "writer" / "bad.json").exists()
    assert recent_tmp.exists()
    assert not stale_tmp.exists()


def test_rejects_unsafe_event_id_without_writing_outside_ack_directory(tmp_path):
    event = _event("unsafe")
    event["event_id"] = "../escape"
    _pending(tmp_path, event, filename="unsafe.json")

    result = EventSubscriber(tmp_path).drain(
        lambda _event: pytest.fail("must not dispatch")
    )

    assert result.malformed == 1
    assert not (tmp_path / "escape.json").exists()


def test_rejects_record_whose_profile_does_not_match_owned_directory(tmp_path):
    path = _pending(tmp_path, _event("wrong-owner", "ops"))
    event = json.loads(path.read_text())
    event["source_profile"] = "writer"
    path.write_text(json.dumps(event), encoding="utf-8")

    result = EventSubscriber(tmp_path).drain(lambda _event: pytest.fail("must not dispatch"))

    assert result.malformed == 1
    assert not (tmp_path / "acknowledged" / "wrong-owner.json").exists()
