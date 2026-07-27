"""Durable consumption for atomic cron lifecycle event files."""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

_msvcrt_locking = getattr(msvcrt, "locking", None)
_msvcrt_lock_nonblocking = getattr(msvcrt, "LK_NBLCK", None)
_msvcrt_unlock = getattr(msvcrt, "LK_UNLCK", None)
_SECURE_DIR_FD = (
    os.name != "nt"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
)


@dataclass
class DrainResult:
    processed: int = 0
    failed: int = 0
    duplicates: int = 0
    malformed: int = 0
    recovered: int = 0
    cleaned: int = 0

    def merge(self, other: "DrainResult") -> None:
        for field in self.__dataclass_fields__:
            setattr(self, field, getattr(self, field) + getattr(other, field))


_PROCESS_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()
_ACTIVE_ROOTS = threading.local()


def _assert_no_symlink_components(path: Path) -> None:
    """Reject existing symlinks in a path before queue side effects."""

    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"cron event path contains symlink component: {current}")


def _assert_regular_file(path: Path) -> None:
    _assert_no_symlink_components(path.parent)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"cron event path is not a regular file: {path}")


@contextmanager
def _open_directory_fd(path: Path) -> Iterator[int | None]:
    """Pin each directory component without following symlinks."""

    if not _SECURE_DIR_FD:
        _assert_no_symlink_components(path)
        yield None
        return
    absolute = Path(os.path.abspath(path))
    active_fds = getattr(_ACTIVE_ROOTS, "fds", {})
    for root_key, root_fd in sorted(
        active_fds.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if root_fd is None:
            continue
        try:
            relative = absolute.relative_to(Path(root_key))
        except ValueError:
            continue
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.dup(root_fd)
        try:
            for part in relative.parts:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)
        return
    _assert_no_symlink_components(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parts = absolute.parts[1:]
    if not parts:
        raise ValueError("cron event root cannot be the filesystem root")
    descriptor = os.open(Path(absolute.anchor) / parts[0], flags)
    try:
        for part in parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _read_regular_bytes(path: Path) -> bytes:
    open_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        open_flags |= os.O_NONBLOCK
    if _SECURE_DIR_FD:
        with _open_directory_fd(path.parent) as parent_fd:
            assert parent_fd is not None
            descriptor = os.open(path.name, open_flags, dir_fd=parent_fd)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"cron event path is not a regular file: {path}")
                with os.fdopen(descriptor, "rb") as handle:
                    descriptor = -1
                    return handle.read()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
    _assert_no_symlink_components(path.parent)
    if path.is_symlink():
        raise ValueError(f"cron event path is a symlink: {path}")
    descriptor = os.open(path, open_flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"cron event path is not a regular file: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _regular_metadata(path: Path) -> os.stat_result:
    """Return metadata for the no-follow, nonblocking regular-file descriptor."""

    open_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        open_flags |= os.O_NONBLOCK
    if _SECURE_DIR_FD:
        with _open_directory_fd(path.parent) as parent_fd:
            assert parent_fd is not None
            descriptor = os.open(path.name, open_flags, dir_fd=parent_fd)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"cron event path is not a regular file: {path}")
                return metadata
            finally:
                os.close(descriptor)
    _assert_no_symlink_components(path.parent)
    descriptor = os.open(path, open_flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"cron event path is not a regular file: {path}")
        return metadata
    finally:
        os.close(descriptor)


def _ensure_directory(path: Path) -> None:
    """Create a private directory hierarchy and persist each new parent entry."""

    if _SECURE_DIR_FD:
        absolute = Path(os.path.abspath(path))
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = None
        walk_parts = None
        active_fds = getattr(_ACTIVE_ROOTS, "fds", {})
        for root_key, root_fd in sorted(
            active_fds.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if root_fd is None:
                continue
            try:
                relative = absolute.relative_to(Path(root_key))
            except ValueError:
                continue
            descriptor = os.dup(root_fd)
            walk_parts = relative.parts
            break
        if descriptor is None:
            _assert_no_symlink_components(path)
            parts = absolute.parts[1:]
            if not parts:
                raise ValueError("cron event root cannot be the filesystem root")
            descriptor = os.open(Path(absolute.anchor) / parts[0], flags)
            walk_parts = parts[1:]
        assert walk_parts is not None
        try:
            for part in walk_parts:
                created = False
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                        created = True
                    except FileExistsError:
                        pass
                    os.fsync(descriptor)
                    child = os.open(part, flags, dir_fd=descriptor)
                if created:
                    try:
                        os.fchmod(child, 0o700)
                    except OSError:
                        pass
                os.close(descriptor)
                descriptor = child
        finally:
            os.close(descriptor)
        return

    _assert_no_symlink_components(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        _fsync_directory(directory.parent)
    _assert_no_symlink_components(path)


def _process_lock(root: Path) -> threading.RLock:
    key = str(Path(os.path.abspath(root)))
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _open_or_create_directory_fd(path: Path) -> Iterator[int | None]:
    """Create missing components and retain the final pinned directory FD."""

    if not _SECURE_DIR_FD:
        _ensure_directory(path)
        with _open_directory_fd(path) as descriptor:
            yield descriptor
        return
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = None
    walk_parts = None
    active_fds = getattr(_ACTIVE_ROOTS, "fds", {})
    for root_key, root_fd in sorted(
        active_fds.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if root_fd is None:
            continue
        try:
            relative = absolute.relative_to(Path(root_key))
        except ValueError:
            continue
        descriptor = os.dup(root_fd)
        walk_parts = relative.parts
        break
    if descriptor is None:
        _assert_no_symlink_components(path)
        parts = absolute.parts[1:]
        if not parts:
            raise ValueError("cron event root cannot be the filesystem root")
        descriptor = os.open(Path(absolute.anchor) / parts[0], flags)
        walk_parts = parts[1:]
    assert walk_parts is not None
    try:
        for part in walk_parts:
            created = False
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    created = True
                except FileExistsError:
                    pass
                os.fsync(descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            if created:
                try:
                    os.fchmod(child, 0o700)
                except OSError:
                    pass
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _pin_active_directory(path: Path, *, create: bool = False) -> Iterator[None]:
    """Keep an intermediate directory inode selected for a whole operation."""

    opener = _open_or_create_directory_fd if create else _open_directory_fd
    with opener(path) as descriptor:
        if descriptor is None:
            yield
            return
        key = str(Path(os.path.abspath(path)))
        active_fds = getattr(_ACTIVE_ROOTS, "fds", {})
        marker = object()
        previous = active_fds.get(key, marker)
        active_fds[key] = descriptor
        _ACTIVE_ROOTS.fds = active_fds
        try:
            yield
        finally:
            if previous is marker:
                active_fds.pop(key, None)
            else:
                active_fds[key] = previous


@contextmanager
def _exclusive_subscriber_lock(root: Path) -> Iterator[None]:
    """Serialize maintenance and delivery across threads and processes."""

    _ensure_directory(root)
    key = str(Path(os.path.abspath(root)))
    active_fds = getattr(_ACTIVE_ROOTS, "fds", {})
    if key in active_fds:
        raise RuntimeError("reentrant cron event subscriber drain is not supported")
    path = root / ".subscriber.lock"
    with _process_lock(root):
        with _open_directory_fd(root) as root_fd:
            if root_fd is None and path.is_symlink():
                raise ValueError(f"cron event lock path is a symlink: {path}")
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if root_fd is None:
                descriptor = os.open(path, flags, 0o600)
            else:
                descriptor = os.open(path.name, flags, 0o600, dir_fd=root_fd)
            with os.fdopen(descriptor, "r+b") as handle:
                try:
                    os.fchmod(handle.fileno(), 0o600)
                except OSError:
                    pass
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b" ")
                    handle.flush()
                    os.fsync(handle.fileno())

                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                elif _msvcrt_locking is not None:  # pragma: no cover - Windows
                    while True:
                        try:
                            handle.seek(0)
                            _msvcrt_locking(
                                handle.fileno(), _msvcrt_lock_nonblocking, 1
                            )
                            break
                        except OSError as exc:
                            if exc.errno not in {
                                errno.EACCES,
                                errno.EAGAIN,
                                errno.EDEADLK,
                            }:
                                raise
                            time.sleep(0.05)
                else:  # pragma: no cover - supported Python platforms provide one
                    raise RuntimeError("cross-process file locking is unavailable")

                try:
                    active_fds[key] = root_fd
                    _ACTIVE_ROOTS.fds = active_fds
                    yield
                finally:
                    active_fds.pop(key, None)
                    if fcntl is not None:
                        try:
                            fcntl.flock(handle, fcntl.LOCK_UN)
                        except OSError:
                            pass
                    elif _msvcrt_locking is not None:  # pragma: no cover - Windows
                        handle.seek(0)
                        try:
                            _msvcrt_locking(handle.fileno(), _msvcrt_unlock, 1)
                        except OSError:
                            pass


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync for durable rename/link/unlink transitions."""

    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _durable_replace(source: Path, destination: Path) -> None:
    _ensure_directory(destination.parent)
    if _SECURE_DIR_FD:
        with _open_directory_fd(source.parent) as source_fd:
            with _open_directory_fd(destination.parent) as destination_fd:
                assert source_fd is not None
                assert destination_fd is not None
                open_flags = os.O_RDONLY | os.O_NOFOLLOW
                if hasattr(os, "O_NONBLOCK"):
                    open_flags |= os.O_NONBLOCK
                probe = os.open(source.name, open_flags, dir_fd=source_fd)
                try:
                    metadata = os.fstat(probe)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ValueError(
                            f"cron event path is not a regular file: {source}"
                        )
                finally:
                    os.close(probe)
                os.replace(
                    source.name,
                    destination.name,
                    src_dir_fd=source_fd,
                    dst_dir_fd=destination_fd,
                )
                os.fsync(destination_fd)
                if source.parent != destination.parent:
                    os.fsync(source_fd)
        return
    _assert_regular_file(source)
    _assert_no_symlink_components(destination.parent)
    os.replace(source, destination)
    _fsync_directory(destination.parent)
    if source.parent != destination.parent:
        _fsync_directory(source.parent)


def _durable_unlink(path: Path) -> None:
    if _SECURE_DIR_FD:
        try:
            with _open_directory_fd(path.parent) as parent_fd:
                assert parent_fd is not None
                os.unlink(path.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
        except FileNotFoundError:
            return
        return
    _assert_no_symlink_components(path.parent)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _durable_touch(path: Path) -> None:
    """Record and persist the start time of processing."""

    if _SECURE_DIR_FD:
        with _open_directory_fd(path.parent) as parent_fd:
            assert parent_fd is not None
            open_flags = os.O_RDONLY | os.O_NOFOLLOW
            if hasattr(os, "O_NONBLOCK"):
                open_flags |= os.O_NONBLOCK
            descriptor = os.open(path.name, open_flags, dir_fd=parent_fd)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"cron event path is not a regular file: {path}")
                os.utime(descriptor, None)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return
    _assert_regular_file(path)
    os.utime(path, None, follow_symlinks=False)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_acknowledge_bytes(payload: bytes, destination: Path) -> None:
    """Atomically create a fresh-inode acknowledgement from already-read bytes."""

    _ensure_directory(destination.parent)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    if _SECURE_DIR_FD:
        with _open_directory_fd(destination.parent) as destination_fd:
            assert destination_fd is not None
            try:
                os.stat(
                    destination.name,
                    dir_fd=destination_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(destination)
            descriptor = os.open(
                temporary.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=destination_fd,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(
                    temporary.name,
                    destination.name,
                    src_dir_fd=destination_fd,
                    dst_dir_fd=destination_fd,
                )
                os.fsync(destination_fd)
            except BaseException:
                try:
                    os.unlink(temporary.name, dir_fd=destination_fd)
                except OSError:
                    pass
                raise
        return
    if os.path.lexists(destination):
        raise FileExistsError(destination)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(
            temporary,
            flags,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        _durable_unlink(temporary)
        raise


def _durable_acknowledge(source: Path, destination: Path) -> None:
    """Atomically create a fresh-inode acknowledgement at success time."""

    _durable_acknowledge_bytes(_read_regular_bytes(source), destination)


def _list_child_names(parent: Path) -> list[str]:
    """List direct child names under a directory, preferring a pinned root FD."""

    if _SECURE_DIR_FD:
        try:
            with _open_directory_fd(parent) as parent_fd:
                if parent_fd is not None:
                    return sorted(os.listdir(parent_fd))
        except (FileNotFoundError, NotADirectoryError, ValueError, OSError):
            return []
    if not parent.is_dir() or parent.is_symlink():
        return []
    return sorted(entry.name for entry in parent.iterdir())


def _list_profile_json_files(stage_root: Path) -> list[Path]:
    """Return profile/*.json paths under a stage root without following swaps."""

    paths: list[Path] = []
    for profile in _list_child_names(stage_root):
        if profile in {".", ".."} or profile.startswith("."):
            continue
        profile_dir = stage_root / profile
        for name in _list_child_names(profile_dir):
            if name.endswith(".json") and not name.startswith("."):
                paths.append(profile_dir / name)
    return sorted(paths)


class EventSubscriber:
    """Claim, dispatch, and acknowledge cron event files durably.

    Delivery is at-least-once across process or power failure: a crash after the
    callback but before acknowledgement can replay the event. A filesystem lock
    covers maintenance, callback execution, and acknowledgement, preventing an
    elapsed recovery timeout from reclaiming an active callback. Successful
    callbacks create a durable acknowledgement before processing state is removed.
    """

    def __init__(
        self,
        root: Path,
        *,
        claim_timeout_seconds: int = 300,
        retention_days: int = 30,
        temporary_retention_seconds: int = 3600,
    ) -> None:
        self.root = Path(root)
        self.claim_timeout_seconds = max(0, claim_timeout_seconds)
        self.retention_days = max(0, retention_days)
        self.temporary_retention_seconds = max(0, temporary_retention_seconds)

    def _pending(self, profile: str) -> Path:
        return self.root / "pending" / profile

    def _processing(self, profile: str) -> Path:
        return self.root / "processing" / profile

    def _quarantine(self, profile: str, name: str) -> Path:
        return self.root / "quarantine" / profile / name

    def _ack(self, event_id: str) -> Path:
        return self.root / "acknowledged" / f"{event_id}.json"

    @staticmethod
    def _profile_for(path: Path) -> str:
        return path.parent.name

    def _retry_destination(self, profile: str, name: str) -> Path:
        return self._pending(profile) / name

    def _recover_stale_processing(self, now: float) -> int:
        recovered = 0
        processing_root = self.root / "processing"
        for profile in _list_child_names(processing_root):
            if profile in {".", ".."} or profile.startswith("."):
                continue
            processing_dir = processing_root / profile
            try:
                with _pin_active_directory(processing_dir):
                    for name in _list_child_names(processing_dir):
                        if not name.endswith(".json") or name.startswith("."):
                            continue
                        path = processing_dir / name
                        try:
                            if (
                                now - _regular_metadata(path).st_mtime
                                < self.claim_timeout_seconds
                            ):
                                continue
                            retry = self._retry_destination(profile, name)
                            with _pin_active_directory(retry.parent, create=True):
                                _durable_replace(path, retry)
                            recovered += 1
                        except (FileNotFoundError, OSError, ValueError):
                            continue
            except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
                continue
        return recovered

    def _cleanup_flat_directory(
        self,
        directory: Path,
        *,
        now: float,
        minimum_age: float,
        predicate: Callable[[str], bool],
    ) -> int:
        cleaned = 0
        try:
            with _pin_active_directory(directory):
                for name in _list_child_names(directory):
                    if not predicate(name):
                        continue
                    target = directory / name
                    try:
                        if now - _regular_metadata(target).st_mtime >= minimum_age:
                            _durable_unlink(target)
                            cleaned += 1
                    except (FileNotFoundError, OSError, ValueError):
                        continue
        except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
            pass
        return cleaned

    def _cleanup_profile_directory(
        self,
        stage_root: Path,
        *,
        now: float,
        minimum_age: float,
        predicate: Callable[[str], bool],
    ) -> int:
        cleaned = 0
        for profile in _list_child_names(stage_root):
            if profile in {".", ".."} or profile.startswith("."):
                continue
            directory = stage_root / profile
            cleaned += self._cleanup_flat_directory(
                directory,
                now=now,
                minimum_age=minimum_age,
                predicate=predicate,
            )
        return cleaned

    def _cleanup_old_files(self, now: float) -> int:
        retention_seconds = self.retention_days * 86400
        cleaned = self._cleanup_flat_directory(
            self.root / "acknowledged",
            now=now,
            minimum_age=retention_seconds,
            predicate=lambda name: name.endswith(".json") and not name.startswith("."),
        )
        cleaned += self._cleanup_profile_directory(
            self.root / "quarantine",
            now=now,
            minimum_age=retention_seconds,
            predicate=lambda name: name.endswith(".json") and not name.startswith("."),
        )
        cleaned += self._cleanup_flat_directory(
            self.root / "claims",
            now=now,
            minimum_age=retention_seconds,
            predicate=lambda name: name.endswith(".claim"),
        )
        cleaned += self._cleanup_profile_directory(
            self.root / "pending",
            now=now,
            minimum_age=self.temporary_retention_seconds,
            predicate=lambda name: name.startswith(".") and name.endswith(".tmp"),
        )
        cleaned += self._cleanup_flat_directory(
            self.root / "acknowledged",
            now=now,
            minimum_age=self.temporary_retention_seconds,
            predicate=lambda name: name.startswith(".") and name.endswith(".tmp"),
        )
        return cleaned

    def _maintain_locked(self) -> DrainResult:
        now = time.time()
        result = DrainResult()
        result.recovered = self._recover_stale_processing(now)
        result.cleaned = self._cleanup_old_files(now)
        return result

    def maintain(self) -> DrainResult:
        """Recover abandoned processing files and enforce retention."""

        with _exclusive_subscriber_lock(self.root):
            return self._maintain_locked()

    @staticmethod
    def _validate(record: object, expected_profile: str) -> Mapping[str, object]:
        if not isinstance(record, dict):
            raise ValueError("event must be a JSON object")
        required = {
            "schema_version",
            "event_id",
            "event_type",
            "emitted_at",
            "source_profile",
            "job_id",
            "job",
        }
        if not required.issubset(record):
            raise ValueError("event is missing required fields")
        if record["schema_version"] != 1:
            raise ValueError("unsupported schema version")
        event_id = record["event_id"]
        if (
            not isinstance(event_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", event_id) is None
        ):
            raise ValueError("invalid event id")
        if record["source_profile"] != expected_profile:
            raise ValueError("source profile does not match publisher directory")
        if not isinstance(record["job"], dict):
            raise ValueError("job must be an object")
        return record

    def _drain_pending(
        self,
        pending: Path,
        profile: str,
        callback: Callable[[Mapping[str, object]], object],
        result: DrainResult,
    ) -> None:
        processing_dir = self._processing(profile)
        with _pin_active_directory(processing_dir, create=True):
            claimed = processing_dir / pending.name
            try:
                _durable_replace(pending, claimed)
                _durable_touch(claimed)
            except FileNotFoundError:
                return
            except (OSError, ValueError):
                result.malformed += 1
                return

            try:
                payload = _read_regular_bytes(claimed)
                record = self._validate(
                    json.loads(payload.decode("utf-8")),
                    profile,
                )
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                quarantine = self._quarantine(profile, claimed.name)
                with _pin_active_directory(quarantine.parent, create=True):
                    _durable_replace(claimed, quarantine)
                result.malformed += 1
                return

            event_id = str(record["event_id"])
            acknowledgement = self._ack(event_id)
            with _pin_active_directory(acknowledgement.parent, create=True):
                try:
                    _read_regular_bytes(acknowledgement)
                except FileNotFoundError:
                    pass
                else:
                    _durable_unlink(claimed)
                    result.duplicates += 1
                    return

                try:
                    callback(record)
                except Exception:
                    retry = self._retry_destination(profile, claimed.name)
                    _durable_replace(claimed, retry)
                    result.failed += 1
                    return

                try:
                    _durable_acknowledge_bytes(payload, acknowledgement)
                except FileExistsError:
                    _durable_unlink(claimed)
                    result.duplicates += 1
                    return
                _durable_unlink(claimed)
                result.processed += 1

    def drain(
        self,
        callback: Callable[[Mapping[str, object]], object],
        *,
        limit: int | None = None,
    ) -> DrainResult:
        """Dispatch pending records and acknowledge only successful callbacks."""

        with _exclusive_subscriber_lock(self.root):
            result = self._maintain_locked()
            remaining = None if limit is None else max(0, int(limit))
            pending_root = self.root / "pending"
            for profile in _list_child_names(pending_root):
                if remaining == 0:
                    break
                if profile in {".", ".."} or profile.startswith("."):
                    continue
                profile_dir = pending_root / profile
                try:
                    with _pin_active_directory(profile_dir):
                        names = [
                            name
                            for name in _list_child_names(profile_dir)
                            if name.endswith(".json") and not name.startswith(".")
                        ]
                        for name in names:
                            if remaining == 0:
                                break
                            self._drain_pending(
                                profile_dir / name,
                                profile,
                                callback,
                                result,
                            )
                            if remaining is not None:
                                remaining -= 1
                except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
                    continue
            return result
